"""
This module implements the linear Kalman filter in both an object
oriented and procedural form. The KalmanFilter class implements
the filter by storing the various matrices in instance variables,
minimizing the amount of bookkeeping you have to do.
All Kalman filters operate with a predict->update cycle. The
predict step, implemented with the method or function predict(),
uses the state transition matrix F to predict the state in the next
time period (epoch). The state is stored as a gaussian (x, P), where
x is the state (column) vector, and P is its covariance. Covariance
matrix Q specifies the process covariance. In Bayesian terms, this
prediction is called the *prior*, which you can think of colloquially
as the estimate prior to incorporating the measurement.
The update step, implemented with the method or function `update()`,
incorporates the measurement z with covariance R, into the state
estimate (x, P). The class stores the system uncertainty in S,
the innovation (residual between prediction and measurement in
measurement space) in y, and the Kalman gain in k. The procedural
form returns these variables to you. In Bayesian terms this computes
the *posterior* - the estimate after the information from the
measurement is incorporated.
Whether you use the OO form or procedural form is up to you. If
matrices such as H, R, and F are changing each epoch, you'll probably
opt to use the procedural form. If they are unchanging, the OO
form is perhaps easier to use since you won't need to keep track
of these matrices. This is especially useful if you are implementing
banks of filters or comparing various KF designs for performance;
a trivial coding bug could lead to using the wrong sets of matrices.
This module also offers an implementation of the RTS smoother, and
other helper functions, such as log likelihood computations.
The Saver class allows you to easily save the state of the
KalmanFilter class after every update.
"""

from __future__ import absolute_import, division

from copy import deepcopy
from math import log, exp, sqrt
import sys
import numpy as np
from numpy import dot, zeros, eye, isscalar, shape
import numpy.linalg as linalg
from filterpy.stats import logpdf
from filterpy.common import pretty_str, reshape_z
from collections import deque


class KalmanFilterXYSR(object):
    """ Implements a Kalman filter. You are responsible for setting the
    various state variables to reasonable values; the defaults will
    not give you a functional filter.
    """

    def __init__(self, dim_x, dim_z, dim_u=0, max_obs=50):
        if dim_x < 1:
            raise ValueError('dim_x must be 1 or greater')
        if dim_z < 1:
            raise ValueError('dim_z must be 1 or greater')
        if dim_u < 0:
            raise ValueError('dim_u must be 0 or greater')

        self.dim_x = dim_x
        self.dim_z = dim_z
        self.dim_u = dim_u

        self.x = zeros((dim_x, 1))        # state
        self.P = eye(dim_x)               # uncertainty covariance
        self.Q = eye(dim_x)               # process uncertainty
        self.B = None                     # control transition matrix
        self.F = eye(dim_x)               # state transition matrix
        self.H = zeros((dim_z, dim_x))    # measurement function
        self.R = eye(dim_z)               # measurement uncertainty
        self._alpha_sq = 1.               # fading memory control
        self.M = np.zeros((dim_x, dim_z)) # process-measurement cross correlation
        self.z = np.array([[None]*self.dim_z]).T

        # gain and residual are computed during the innovation step. We
        # save them so that in case you want to inspect them for various
        # purposes
        self.K = np.zeros((dim_x, dim_z)) # kalman gain
        self.y = zeros((dim_z, 1))
        self.S = np.zeros((dim_z, dim_z)) # system uncertainty
        self.SI = np.zeros((dim_z, dim_z)) # inverse system uncertainty

        # identity matrix. Do not alter this.
        self._I = np.eye(dim_x)

        # these will always be a copy of x,P after predict() is called
        self.x_prior = self.x.copy()
        self.P_prior = self.P.copy()

        # these will always be a copy of x,P after update() is called
        self.x_post = self.x.copy()             
        self.P_post = self.P.copy()

        # Only computed only if requested via property
        self._log_likelihood = log(sys.float_info.min)
        self._likelihood = sys.float_info.min
        self._mahalanobis = None

        # keep all observations 
        self.max_obs = max_obs
        self.history_obs = deque([], maxlen=self.max_obs)

        self.inv = np.linalg.inv

        self.attr_saved = None
        self.observed = False
        
    def apply_affine_correction(self, m, t, new_kf):
        """
        Apply to both last state and last observation for OOS smoothing.

        Messy due to internal logic for kalman filter being messy.
        """
        if new_kf:
            big_m = np.kron(np.eye(4, dtype=float), m)
            self.x = big_m @ self.x
            self.x[:2] += t
            self.P = big_m @ self.P @ big_m.T

            # If frozen, also need to update the frozen state for OOS
            if not self.observed and self.attr_saved is not None:
                self.attr_saved["x"] = big_m @ self.attr_saved["x"]
                self.attr_saved["x"][:2] += t
                self.attr_saved["P"] = big_m @ self.attr_saved["P"] @ big_m.T
                self.attr_saved["last_measurement"][:2] = m @ self.attr_saved["last_measurement"][:2] + t
                self.attr_saved["last_measurement"][2:] = m @ self.attr_saved["last_measurement"][2:]
        else:
            scale = np.linalg.norm(m[:, 0])
            self.x[:2] = m @ self.x[:2] + t
            self.x[4:6] = m @ self.x[4:6]
            # self.x[2] *= scale
            # self.x[6] *= scale

            self.P[:2, :2] = m @ self.P[:2, :2] @ m.T
            self.P[4:6, 4:6] = m @ self.P[4:6, 4:6] @ m.T
            # self.P[2, 2] *= 2 * scale
            # self.P[6, 6] *= 2 * scale

            # If frozen, also need to update the frozen state for OOS
            if not self.observed and self.attr_saved is not None:
                self.attr_saved["x"][:2] = m @ self.attr_saved["x"][:2] + t
                self.attr_saved["x"][4:6] = m @ self.attr_saved["x"][4:6]
                # self.attr_saved["x"][2] *= scale
                # self.attr_saved["x"][6] *= scale

                self.attr_saved["P"][:2, :2] = m @ self.attr_saved["P"][:2, :2] @ m.T
                self.attr_saved["P"][4:6, 4:6] = m @ self.attr_saved["P"][4:6, 4:6] @ m.T
                # self.attr_saved["P"][2, 2] *= 2 * scale
                # self.attr_saved["P"][6, 6] *= 2 * scale

                self.attr_saved["last_measurement"][:2] = m @ self.attr_saved["last_measurement"][:2] + t
                # self.attr_saved["last_measurement"][2] *= scale

    def predict(self, u=None, B=None, F=None, Q=None):
        """
        Predict next state (prior) using the Kalman filter state propagation
        equations.
        Parameters
        ----------
        u : np.array, default 0
            Optional control vector.
        B : np.array(dim_x, dim_u), or None
            Optional control transition matrix; a value of None
            will cause the filter to use `self.B`.
        F : np.array(dim_x, dim_x), or None
            Optional state transition matrix; a value of None
            will cause the filter to use `self.F`.
        Q : np.array(dim_x, dim_x), scalar, or None
            Optional process noise matrix; a value of None will cause the
            filter to use `self.Q`.
        """
        if B is None:
            B = self.B
        if F is None:
            F = self.F
        if Q is None:
            Q = self.Q
        elif isscalar(Q):
            Q = eye(self.dim_x) * Q

        # x = Fx + Bu
        if B is not None and u is not None:
            self.x = dot(F, self.x) + dot(B, u)
        else:
            self.x = dot(F, self.x)

        # P = FPF' + Q
        self.P = self._alpha_sq * dot(dot(F, self.P), F.T) + Q

        # save prior
        self.x_prior = self.x.copy()
        self.P_prior = self.P.copy()

    def freeze(self):
        """
            Save the parameters before non-observation forward
        """
        self.attr_saved = deepcopy(self.__dict__)

    def unfreeze(self):
        if self.attr_saved is not None:
            new_history = deepcopy(list(self.history_obs))
            self.__dict__ = self.attr_saved
            self.history_obs = deque(list(self.history_obs)[:-1], maxlen=self.max_obs)
            occur = [int(d is None) for d in new_history]
            indices = np.where(np.array(occur) == 0)[0]
            index1, index2 = indices[-2], indices[-1]
            box1, box2 = new_history[index1], new_history[index2]
            x1, y1, s1, r1 = box1
            w1, h1 = np.sqrt(s1 * r1), np.sqrt(s1 / r1)
            x2, y2, s2, r2 = box2
            w2, h2 = np.sqrt(s2 * r2), np.sqrt(s2 / r2)
            time_gap = index2 - index1
            dx, dy = (x2 - x1) / time_gap, (y2 - y1) / time_gap
            dw, dh = (w2 - w1) / time_gap, (h2 - h1) / time_gap

            for i in range(index2 - index1):
                x, y = x1 + (i + 1) * dx, y1 + (i + 1) * dy
                w, h = w1 + (i + 1) * dw, h1 + (i + 1) * dh
                s, r = w * h, w / float(h)
                new_box = np.array([x, y, s, r]).reshape((4, 1))
                self.update(new_box)
                if not i == (index2 - index1 - 1):
                    self.predict()
                    self.history_obs.pop()
            self.history_obs.pop()

    def update(self, z, R=None, H=None):
        """
        Add a new measurement (z) to the Kalman filter. If z is None, nothing is changed.
        Parameters
        ----------
        z : np.array
            Measurement for this update. z can be a scalar if dim_z is 1,
            otherwise it must be a column vector.
        R : np.array, scalar, or None
            Measurement noise. If None, the filter's self.R value is used.
        H : np.array, or None
            Measurement function. If None, the filter's self.H value is used.
        """
        if z is None:
            self.history_obs.append(z)
            self.observed = False
            return

        self.observed = True
        if R is None:
            R = self.R
        if H is None:
            H = self.H
        H = np.asarray(H)

        # y = z - Hx
        # error (residual) between measurement and prediction
        z = reshape_z(z, self.dim_z, self.x.ndim)
        self.y = z - dot(H, self.x)

        # common subexpression for speed
        PHT = dot(self.P, H.T)

        # S = HPH' + R
        self.S = dot(H, PHT) + R
        self.SI = self.inv(self.S)

        # K = PH'inv(S)
        self.K = PHT.dot(self.SI)

        # x = x + Ky
        self.x = self.x + dot(self.K, self.y)

        # P = (I-KH)P(I-KH)' + KRK'
        I_KH = self._I - dot(self.K, H)
        self.P = dot(dot(I_KH, self.P), I_KH.T) + dot(dot(self.K, R), self.K.T)

        # save measurement and posterior state
        self.z = deepcopy(z)
        self.x_post = self.x.copy()
        self.P_post = self.P.copy()

        # save history of observations
        self.history_obs.append(z)

    def update_steadystate(self, z, H=None):
        """ Update Kalman filter using the Kalman gain and state covariance
        matrix as computed for the steady state. Only x is updated, and the
        new value is stored in self.x. P is left unchanged. Must be called
        after a prior call to compute_steady_state().
        """
        if z is None:
            self.history_obs.append(z)
            return

        if H is None:
            H = self.H

        H = np.asarray(H)
        # error (residual) between measurement and prediction
        self.y = z - dot(H, self.x)

        # x = x + Ky
        self.x = self.x + dot(self.K_steady_state, self.y)

        # save measurement and posterior state
        self.z = deepcopy(z)
        self.x_post = self.x.copy()

        # save history of observations
        self.history_obs.append(z)

    def log_likelihood(self, z=None):
        """ log-likelihood of the measurement z. Computed from the
        system uncertainty S.
        """

        if z is None:
            z = self.z
        return logpdf(z, dot(self.H, self.x), self.S)

    def likelihood(self, z=None):
        """ likelihood of the measurement z. Computed from the
        system uncertainty S.
        """

        if z is None:
            z = self.z
        return exp(self.log_likelihood(z))

    @property
    def log_likelihood(self):
        """ log-likelihood of the last measurement.
        """

        return self._log_likelihood

    @property
    def likelihood(self):
        """ likelihood of the last measurement.
        """

        return self._likelihood

    def rts_smoother(self, Xs, Ps, Fs=None, Qs=None):
        """ Runs the Rauch-Tung-Striebel (RTS) smoother on a set of
        Kalman filter output, consisting of means and covariances.
        """

        if len(Xs) != len(Ps):
            raise ValueError("length of Xs and Ps must be equal")

        n = len(Xs)
        dim_x = Xs[0].shape[0]

        if Fs is None:
            Fs = [self.F] * n
        if Qs is None:
            Qs = [self.Q] * n

        # smoother gain
        K = np.zeros((n, dim_x, dim_x))

        x, P = Xs.copy(), Ps.copy()
        for k in range(n-2, -1, -1):
            P_pred = dot(dot(Fs[k], P[k]), Fs[k].T) + Qs[k]
            K[k] = dot(dot(P[k], Fs[k].T), self.inv(P_pred))
            x[k] += dot(K[k], x[k+1] - dot(Fs[k], x[k]))
            P[k] += dot(dot(K[k], P[k+1] - P_pred), K[k].T)
        return (x, P, K)

    def batch_filter(self, zs, us=None, Bs=None, Fs=None, Qs=None, Hs=None, Rs=None):
        """ Batch processes a sequence of measurements.

        Parameters
        ----------
        zs : list-like
            list of measurements at each time step. Missing measurements must be
            represented by NaNs.

        us : list-like, optional, default=None
            If not None, contains control inputs for each time step. If
            None, defers to self.u.

        Bs : list-like, optional, default=None
            If not None, contains the control transition matrix for each time
            step. If None, defers to self.B.

        Fs : list-like, optional, default=None
            If not None, contains the state transition matrix for each time
            step. If None, defers to self.F.

        Qs : list-like, optional, default=None
            If not None, contains the process noise matrix for each time step.
            If None, defers to self.Q.

        Hs : list-like, optional, default=None
            If not None, contains the measurement function matrix for each time
            step. If None, defers to self.H.

        Rs : list-like, optional, default=None
            If not None, contains the measurement noise matrix for each time step.
            If None, defers to self.R.

        Returns
        -------
        means : np.array((n,dim_x))
            array of the state for each time step
        covariances : np.array((n,dim_x,dim_x))
            array of the covariance matrix for each time step
        """

        n = np.size(zs, 0)

        if us is None:
            us = [0.] * n
        if Bs is None:
            Bs = [self.B] * n
        if Fs is None:
            Fs = [self.F] * n
        if Qs is None:
            Qs = [self.Q] * n
        if Hs is None:
            Hs = [self.H] * n
        if Rs is None:
            Rs = [self.R] * n

        # mean estimates from Kalman filter
        means = np.zeros((n, self.dim_x, 1))

        # state covariances from Kalman filter
        covariances = np.zeros((n, self.dim_x, self.dim_x))

        for i, (z, u, B, F, Q, H, R) in enumerate(zip(zs, us, Bs, Fs, Qs, Hs, Rs)):
            self.predict(u=u, B=B, F=F, Q=Q)
            self.update(z=z, H=H, R=R)

            means[i, :] = self.x
            covariances[i, :, :] = self.P

        return (means, covariances)

    def batch_filter(self, zs, Rs=None):
        """
        Batch process a sequence of measurements. This method is suitable
        for cases where the measurement noise varies with each measurement.
        """
        means, covariances = [], []
        for z, R in zip(zs, Rs):
            self.predict()
            self.update(z, R=R)
            means.append(self.x.copy())
            covariances.append(self.P.copy())
        return np.array(means), np.array(covariances)