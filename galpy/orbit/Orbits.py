import os
import copy
import warnings
import numpy
from .Orbit import Orbit
from ..util import galpyWarning, galpyWarningVerbose
from ..util.multi import parallel_map
from ..util.bovy_plot import _add_ticks
from ..potential.Potential import _check_c
from ..potential.DissipativeForce import _isDissipative
from .integrateLinearOrbit import integrateLinearOrbit_c, _ext_loaded
from .integratePlanarOrbit import integratePlanarOrbit_c
from .integrateFullOrbit import integrateFullOrbit_c
ext_loaded= _ext_loaded
try:
    from astropy.coordinates import SkyCoord
    _APY_LOADED = True
except ImportError:
    SkyCoord = None
    _APY_LOADED = False
# Set default numcores for integrate w/ parallel map using OMP_NUM_THREADS
try:
    _NUMCORES= int(os.environ['OMP_NUM_THREADS'])
except KeyError:
    import multiprocessing
    _NUMCORES= multiprocessing.cpu_count()
class Orbits(object):
    """
    Class representing multiple orbits.
    """
    def __init__(self, vxvv=None, radec=False, uvw=False, lb=False, ro=None,
                 vo=None, zo=None, solarmotion=None):
        """
        NAME:

            __init__

        PURPOSE:

            Initialize an Orbits instance

        INPUT:

            vxvv - initial conditions; can be either

                a) list of Orbit instances

                b) astropy (>v3.0) SkyCoord including velocities (note that this turns *on* physical output even if ro and vo are not given)

                c) list of initial conditions for individual Orbit instances; elements can be either

                    1) in Galactocentric cylindrical coordinates [R,vR,vT(,z,vz,phi)]; can be Quantities

                    2) [ra,dec,d,mu_ra, mu_dec,vlos] in [deg,deg,kpc,mas/yr,mas/yr,km/s] (all J2000.0; mu_ra = mu_ra * cos dec); can be Quantities; ICRS frame

                    3) [ra,dec,d,U,V,W] in [deg,deg,kpc,km/s,km/s,kms]; can be Quantities; ICRS frame

                    4) [l,b,d,mu_l,mu_b,vlos] in [deg,deg,kpc,mas/yr,mas/yr,km/s) (all J2000.0; mu_l = mu_l * cos b); can be Quantities

                    5) [l,b,d,U,V,W] in [deg,deg,kpc,km/s,km/s,kms]; can be Quantities

                    6) None: assumed to be the Sun (equivalent to ``[0,0,0,0,0,0]`` and ``radec=True``)

                4) and 5) also work when leaving out b and mu_b/W

        OPTIONAL INPUTS:

            radec - if True, input is 2) or 3) above (note that this turns *on* physical output even if ro and vo are not given)

            uvw - if True, velocities are UVW

            lb - if True, input is 4) or 5) above (note that this turns *on* physical output even if ro and vo are not given)

            ro - distance from vantage point to GC (kpc; can be Quantity)

            vo - circular velocity at ro (km/s; can be Quantity)

            zo - offset toward the NGP of the Sun wrt the plane (kpc; can be Quantity; default = 25 pc)

            solarmotion - 'hogg' or 'dehnen', or 'schoenrich', or value in [-U,V,W]; can be Quantity

        OUTPUT:

            instance

        HISTORY:

            2018-10-13 - Written - Mathew Bub (UofT)

        """
        if vxvv is None:
            vxvv = []

        if _APY_LOADED and isinstance(vxvv, SkyCoord):
            self._orbits = [Orbit(vxvv=coord) for coord in vxvv.flatten()]
        else:
            self._orbits = []
            for coord in vxvv:
                if isinstance(coord, Orbit):
                    self._orbits.append(coord)
                else:
                    orbit = Orbit(vxvv=coord, radec=radec, uvw=uvw, lb=lb,
                                  ro=ro, vo=vo, zo=zo, solarmotion=solarmotion)
                    self._orbits.append(orbit)

    def __len__(self):
        return len(self._orbits)

    def __getattr__(self, name):
        """
        NAME:

            __getattr__

        PURPOSE:

            get or evaluate an attribute for these Orbits

        INPUT:

            name - name of the attribute

        OUTPUT:

            if the attribute is callable, a function to evaluate the attribute for each Orbit; otherwise a list of attributes

        HISTORY:

            2018-10-13 - Written - Mathew Bub (UofT)

        """
        attribute = getattr(Orbit(), name)
        if callable(attribute):
            return lambda *args, **kwargs: [
                getattr(orbit, name)(*args, **kwargs) for orbit in self._orbits
            ]
        else:
            return [getattr(orbit, name) for orbit in self.orbits]

    def __getitem__(self,key):
        """
        NAME:

            __getitem__

        PURPOSE:

            get a subset of this instance's orbits

        INPUT:

           key - slice

        OUTPUT:

           For single item: Orbit instance, for multiple items: another Orbits instance

        HISTORY:

            2018-12-31 - Written - Bovy (UofT)

        """
        if isinstance(key,int):
            if key < 0 : # negative indices
                key+= len(self)
            return copy.deepcopy(self._orbits[key])
        elif isinstance(key,slice):
            orbits_list= [copy.deepcopy(self._orbits[ii]) 
                          for ii in range(*key.indices(len(self)))]
            if hasattr(self,'orbit'):
                integrated_orbits= copy.deepcopy(self.orbit[key])
            else: integrated_orbits= None
            return Orbits._from_slice(orbits_list,integrated_orbits)

    @classmethod
    def _from_slice(cls,orbits_list,integrated_orbits=None):
        out= cls(vxvv=orbits_list)
        if not integrated_orbits is None:
            out.orbits= integrated_orbits
        return out

    def integrate(self,t,pot,method='symplec4_c',dt=None,numcores=_NUMCORES,
                  force_map=False):
        """
        NAME:

            integrate

        PURPOSE:

            integrate these Orbits with multiprocessing

        INPUT:

            t - list of times at which to output (0 has to be in this!) (can be Quantity)

            pot - potential instance or list of instances

            method = 'odeint' for scipy's odeint
                     'leapfrog' for a simple leapfrog implementation
                     'leapfrog_c' for a simple leapfrog implementation in C
                     'symplec4_c' for a 4th order symplectic integrator in C
                     'symplec6_c' for a 6th order symplectic integrator in C
                     'rk4_c' for a 4th-order Runge-Kutta integrator in C
                     'rk6_c' for a 6-th order Runge-Kutta integrator in C
                     'dopr54_c' for a 5-4 Dormand-Prince integrator in C
                     'dopr853_c' for a 8-5-3 Dormand-Prince integrator in C

            dt - if set, force the integrator to use this basic stepsize; must be an integer divisor of output stepsize (only works for the C integrators that use a fixed stepsize) (can be Quantity)

            numcores - number of cores to use for multiprocessing with force_map; default = 1

        OUTPUT:

            None (get the actual orbit using getOrbit())

        HISTORY:

            2018-10-13 - Written as parallel_map applied to regular Orbit integration - Mathew Bub (UofT)

        """
        # Need to add checks done in Orbit.integrate

        if hasattr(self,'_orbInterp'): delattr(self,'_orbInterp')
        self.t= numpy.array(t)
        self._pot= pot

        #First check that the potential has C
        if '_c' in method:
            if not ext_loaded or not _check_c(pot):
                if ('leapfrog' in method or 'symplec' in method):
                    method= 'leapfrog'
                else:
                    method= 'odeint'
                if not ext_loaded: # pragma: no cover
                    warnings.warn("Cannot use C integration because C extension not loaded (using %s instead)" % (method), galpyWarning)
                else:
                    warnings.warn("Cannot use C integration because some of the potentials are not implemented in C (using %s instead)" % (method), galpyWarning)
        # Now check that we aren't trying to integrate a dissipative force
        # with a symplectic integrator
        if _isDissipative(pot) and ('leapfrog' in method 
                                    or 'symplec' in method):
            if '_c' in method:
                method= 'dopr54_c'
            else:
                method= 'odeint'
            warnings.warn("Cannot use symplectic integration because some of the included forces are dissipative (using non-symplectic integrator %s instead)" % (method), galpyWarning)
        # Implementation with parallel_map in Python
        if not '_c' in method or not ext_loaded or force_map:
            # Must return each Orbit for its values to correctly update
            def integrate_for_map(orbit):
                orbit.integrate(t, pot, method=method, dt=dt)
                return orbit
            self._orbits = list(parallel_map(integrate_for_map, self._orbits,
                                             numcores=numcores))
            # Gather all into single self.orbit array
            self.orbit= numpy.array([self._orbits[ii]._orb.orbit
                                     for ii in range(len(self))])
        else:
            warnings.warn("Using C implementation to integrate orbits",
                          galpyWarningVerbose)
            if self._orbits[0].dim() == 1:
                vxvvs= numpy.array([o._orb.vxvv for o in self._orbits])
                out, msg= integrateLinearOrbit_c(pot,numpy.copy(vxvvs),
                                                 t,method,dt=dt)
            else:
                if self._orbits[0].phasedim() == 3 \
                   or self._orbits[0].phasedim() == 5:
                    #We hack this by putting in a dummy phi=0
                    vxvvs= numpy.array([numpy.hstack((o._orb.vxvv,0.))
                                        for o in self._orbits])
                else:
                    vxvvs= numpy.array([o._orb.vxvv for o in self._orbits])
                if self._orbits[0].dim() == 2:
                    out, msg= integratePlanarOrbit_c(pot,numpy.copy(vxvvs),
                                                     t,method,dt=dt)
                else:
                    out, msg= integrateFullOrbit_c(pot,numpy.copy(vxvvs),
                                                   t,method,dt=dt)

                if self._orbits[0].phasedim() == 3 \
                   or self._orbits[0].phasedim() == 5:
                    out= out[:,:,:-1]
            # Store orbit internally
            self.orbit= out
        # Also store per-orbit view of the orbit for __getattr__ funcs
        for ii in range(len(self)):
            self._orbits[ii]._orb.orbit= self.orbit[ii]
            self._orbits[ii]._orb.t= t
        return None

    def plot(self,*args,**kwargs):
        """
        Like Orbit.plot but for Orbits, same exact calling sequence
        Written - 2018-12-19 - Bovy (UofT)"""
        for ii in range(len(self)):
            line2d= self._orbits[ii].plot(*args,**kwargs)[0]
            kwargs['overplot']= True
        line2d.axes.autoscale(enable=True)
        _add_ticks()
        return None
