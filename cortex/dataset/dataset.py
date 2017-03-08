import tempfile
import warnings
import numpy as np
import h5py

from ..database import db
from ..xfm import Transform

from .braindata import _hdf_write
from .views import normalize as _vnorm
from .views import Dataview

class Dataset(object):
    """
    Wrapper for multiple data objects. This often does not need to be used 
    explicitly--for example, if a dictionary of data objects is passed to 
    `cortex.webshow`, it will automatically be converted into a `Dataset`.

    All kwargs should be `BrainData` or `Dataset` objects.
    """
    def __init__(self, **kwargs):
        self.h5 = None
        self.views = {}

        self.append(**kwargs)

    def append(self, **kwargs):
        """Add the `BrainData` or `Dataset` objects in `kwargs` into this 
        dataset.
        """
        for name, data in kwargs.items():
            norm = normalize(data)

            if isinstance(norm, Dataview):
                self.views[name] = norm
            elif isinstance(norm, Dataset):
                self.views.update(norm.views)
            else:
                raise ValueError("Unknown input %s=%r"%(name, data))

        return self

    def __getattr__(self, attr):
        if attr in self.__dict__:
            return self.__dict__[attr]
        elif attr in self.views:
            return self.views[attr]

        raise AttributeError

    def __getitem__(self, item):
        return self.views[item]

    def __iter__(self):
        for name, dv in sorted(self.views.items(), key=lambda x: x[1].priority):
            yield name, dv

    def __repr__(self):
        views = sorted(self.views.items(), key=lambda x: x[1].priority)
        return "<Dataset with views [%s]>"%(', '.join([n for n, d in views]))

    def __len__(self):
        return len(self.views)

    def __dir__(self):
        return list(self.__dict__.keys()) + list(self.views.keys())

    @classmethod
    def from_file(cls, filename):
        ds = cls()
        ds.h5 = h5py.File(filename)

        db.auxfile = ds

        #detect stray datasets which were not written by pycortex
        for name, node in ds.h5.items():
            if name in ("data", "subjects", "views"):
                continue
            try:
                ds.views[name] = _from_hdf_data(ds.h5, name)
            except KeyError:
                print('No metadata found for "%s", skipping...'%name)

        #load up the views generated by pycortex
        for name, node in ds.h5['views'].items():
            try:
                ds.views[name] = Dataview.from_hdf(node)
            except:
                import traceback
                traceback.print_exc()

        db.auxfile = None

        return ds
        
    def uniques(self, collapse=False):
        """Return the set of unique BrainData objects contained by this dataset"""
        uniques = set()
        for name, view in self:
            for sv in view.uniques(collapse=collapse):
                uniques.add(sv)

        return uniques

    def save(self, filename=None, pack=False):
        if filename is not None:
            self.h5 = h5py.File(filename)
        elif self.h5 is None:
            raise ValueError("Must provide filename for new datasets")

        for name, view in self.views.items():
            view._write_hdf(self.h5, name=name)
            
        if pack:
            subjs = set()
            xfms = set()
            masks = set()
            for view in self.views.values():
                for data in view.uniques():
                    subjs.add(data.subject)
                    if isinstance(data, Volume):
                        xfms.add((data.subject, data.xfmname))
                        #custom masks are already packaged by default
                        #only string masks need to be packed
                        if isinstance(data._mask, str):
                            masks.add((data.subject, data.xfmname, data._mask))

            _pack_subjs(self.h5, subjs)
            _pack_xfms(self.h5, xfms)
            _pack_masks(self.h5, masks)

        self.h5.flush()

    def get_surf(self, subject, type, hemi='both', merge=False, nudge=False):
        if hemi == 'both':
            left = self.get_surf(subject, type, "lh", nudge=nudge)
            right = self.get_surf(subject, type, "rh", nudge=nudge)
            if merge:
                pts = np.vstack([left[0], right[0]])
                polys = np.vstack([left[1], right[1]+len(left[0])])
                return pts, polys

            return left, right
        try:
            if type == 'fiducial':
                wpts, polys = self.get_surf(subject, 'wm', hemi)
                ppts, _     = self.get_surf(subject, 'pia', hemi)
                return (wpts + ppts) / 2, polys

            group = self.h5['subjects'][subject]['surfaces'][type][hemi]
            pts, polys = group['pts'].value.copy(), group['polys'].value.copy()
            if nudge:
                if hemi == 'lh':
                    pts[:,0] -= pts[:,0].max()
                else:
                    pts[:,0] -= pts[:,0].min()
            return pts, polys
        except (KeyError, TypeError):
            raise IOError('Subject not found in package')

    def get_xfm(self, subject, xfmname):
        try:
            group = self.h5['subjects'][subject]['transforms'][xfmname]
            return Transform(group['xfm'].value, tuple(group['xfm'].attrs['shape']))
        except (KeyError, TypeError):
            raise IOError('Transform not found in package')

    def get_mask(self, subject, xfmname, maskname):
        try:
            group = self.h5['subjects'][subject]['transforms'][xfmname]['masks']
            return group[maskname]
        except (KeyError, TypeError):
            raise IOError('Mask not found in package')

    def get_overlay(self, subject, type='rois', **kwargs):
        try:
            group = self.h5['subjects'][subject]
            if type == "rois":
                tf = tempfile.NamedTemporaryFile()
                tf.write(group['rois'][0])
                tf.seek(0)
                return tf
        except (KeyError, TypeError):
            raise IOError('Overlay not found in package')

        raise TypeError('Unknown overlay type')

    def prepend(self, prefix):
        """Adds the given `prefix` to the name of every data object and returns
        a new Dataset.
        """
        ds = dict()
        for name, data in self:
            ds[prefix+name] = data

        return Dataset(**ds)

def normalize(data):
    if isinstance(data, (Dataset, Dataview)):
        return data
    elif isinstance(data, dict):
        return Dataset(**data)
    elif isinstance(data, str):
        return Dataset.from_file(data)
    elif isinstance(data, tuple):
        return _vnorm(data)

    raise TypeError('Unknown input type')

def _pack_subjs(h5, subjects):
    for subject in subjects:
        rois = db.get_overlay(subject, type='rois')
        rnode = h5.require_dataset("/subjects/%s/rois"%subject, (1,),
            dtype=h5py.special_dtype(vlen=str))
        rnode[0] = rois.toxml(pretty=False)

        surfaces = db.get_paths(subject)['surfs']
        for surf in surfaces.keys():
            for hemi in ("lh", "rh"):
                pts, polys = db.get_surf(subject, surf, hemi)
                group = "/subjects/%s/surfaces/%s/%s"%(subject, surf, hemi)
                _hdf_write(h5, pts, "pts", group)
                _hdf_write(h5, polys, "polys", group)

def _pack_xfms(h5, xfms):
    for subj, xfmname in xfms:
        xfm = db.get_xfm(subj, xfmname, 'coord')
        group = "/subjects/%s/transforms/%s"%(subj, xfmname)
        node = _hdf_write(h5, np.array(xfm.xfm), "xfm", group)
        node.attrs['shape'] = xfm.shape

def _pack_masks(h5, masks):
    for subj, xfm, maskname in masks:
        mask = db.get_mask(subj, xfm, maskname)
        group = "/subjects/%s/transforms/%s/masks"%(subj, xfm)
        _hdf_write(h5, mask, maskname, group)
