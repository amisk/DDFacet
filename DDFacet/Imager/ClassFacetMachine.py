'''
DDFacet, a facet-based radio imaging package
Copyright (C) 2013-2016  Cyril Tasse, l'Observatoire de Paris,
SKA South Africa, Rhodes University

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
'''

import ClassDDEGridMachine
import numpy as np
import ClassCasaImage
import pyfftw
from DDFacet.Array import NpShared
from DDFacet.Imager.ClassImToGrid import ClassImToGrid
from DDFacet.Other import ClassTimeIt
from DDFacet.Other import MyLogger
from DDFacet.Other.progressbar import ProgressBar
import cPickle
import atexit
import traceback
from matplotlib.path import Path
import pylab
import numpy.random
from DDFacet.ToolsDir import ModCoord
from DDFacet.Array import NpShared
from DDFacet.Array import SharedDict
from DDFacet.ToolsDir import ModFFTW
from DDFacet.Other import ClassTimeIt
from DDFacet.Other import Multiprocessing
from DDFacet.Other import ModColor
from DDFacet.ToolsDir.ModToolBox import EstimateNpix
from DDFacet.ToolsDir.GiveEdges import GiveEdges
from DDFacet.Imager.ClassImToGrid import ClassImToGrid
from DDFacet.Other import MyLogger
from DDFacet.cbuild.Gridder import _pyGridderSmearPols
#from DDFacet.Array import NpParallel
log=MyLogger.getLogger("ClassFacetMachine")
from DDFacet.Other.AsyncProcessPool import APP

MyLogger.setSilent("MyLogger")
from DDFacet.cbuild.Gridder import _pyGridderSmearPols
import DDFacet.Data.ClassBeamMean as ClassBeamMean
from DDFacet.Other import ModColor
MyLogger.setSilent("MyLogger")

class ClassFacetMachine():
    """
    This class contains all information about facets and projections.
    The class is responsible for tesselation, gridding, projection to image,
    unprojection to facets and degridding

    This class provides a basic gridded tesselation pattern.
    """

    def __init__(self,
                 VS,
                 GD,
                 # ParsetFile="ParsetNew.txt",
                 Precision="S",
                 PolMode="I",
                 Sols=None,
                 PointingID=0,
                 DoPSF=False,
                 Oversize=1,   # factor by which image is oversized
                 ):

        self.HasFourierTransformed = False

        if Precision == "S":
            self.dtype = np.complex64
            self.CType = np.complex64
            self.FType = np.float32
            self.stitchedType = np.float32  # cleaning requires float32
        elif Precision == "D":
            self.dtype = np.complex128
            self.CType = np.complex64
            self.FType = np.float64
            self.stitchedType = np.float32  # cleaning requires float32

        self.DoDDE = False
        if Sols is not None:
            self.setSols(Sols)

        self.PointingID = PointingID
        self.VS, self.GD = VS, GD
        self.npol = self.VS.StokesConverter.NStokesInImage()
        self.Parallel = True
        if APP is not None:
            APP.registerJobHandlers(self)
            self._fft_job_counter = APP.createJobCounter("fft")
            self._app_id = "FMPSF" if DoPSF else "FM"

        DicoConfigGM = {}
        self.DicoConfigGM = DicoConfigGM
        self.DoPSF = DoPSF
        # self.MDC.setFreqs(ChanFreq)
        self.CasaImage = None
        self.IsDirtyInit = False
        self.IsDDEGridMachineInit = False
        self.SharedNames = []
        self.ConstructMode = GD["RIME"]["ConstructMode"]
        self.SpheNorm = True

        if self.ConstructMode == "Fader":
            self.SpheNorm = False
        else:
            raise RuntimeError(
                "Deprecated Facet construct mode. Only supports 'Fader'")
        self.Oversize = Oversize

        DecorrMode=self.GD["RIME"]["DecorrMode"]
        if DecorrMode is not None and DecorrMode is not "":
            print>>log,ModColor.Str("Using decorrelation mode %s"%DecorrMode)
        self.AverageBeamMachine=None
        self.DoComputeSmoothBeam=False
        self.SmoothMeanNormImage=None
        self.NormData = None
        self.NormImage = None
        self._facet_grids = self.DATA = None
        self._grid_job_id = self._fft_job_id = self._degrid_job_id = None

        # create semaphores if not already created
        if not ClassFacetMachine._degridding_semaphores:
            NSemaphores = 3373
            ClassFacetMachine._degridding_semaphores = [Multiprocessing.getShmName("Semaphore", sem=i) for i in xrange(NSemaphores)]
            _pyGridderSmearPols.pySetSemaphores(ClassFacetMachine._degridding_semaphores)
            atexit.register(ClassFacetMachine._delete_degridding_semaphores)

        # this is used to store NormImage and model images in shared memory, for the degridder
        self._model_dict = None

    # static attribute initialized below, once
    _degridding_semaphores = None

    @staticmethod
    def _delete_degridding_semaphores():
        if ClassFacetMachine._degridding_semaphores:
            _pyGridderSmearPols.pyDeleteSemaphore(ClassFacetMachine._degridding_semaphores)
            for sem in ClassFacetMachine._degridding_semaphores:
                NpShared.DelArray(sem)

    def __del__(self):
        # print>>log,"Deleting shared memory"
        if self._facet_grids:
            self._facet_grids.delete()
            del self.DicoGridMachine



    def SetLogModeSubModules(self,Mode="Silent"):
        SubMods=["ModelBeamSVD","ClassParam","ModToolBox","ModelIonSVD2","ClassPierce"]

        if Mode == "Silent":
            MyLogger.setSilent(SubMods)
        if Mode == "Loud":
            MyLogger.setLoud(SubMods)

    def setSols(self, SolsClass):
        self.DoDDE = True
        self.Sols = SolsClass

    def appendMainField(self, Npix=512, Cell=10., NFacets=5,
                        Support=11, OverS=5, Padding=1.2,
                        wmax=10000, Nw=11, RaDecRad=(0., 0.),
                        ImageName="Facet.image", **kw):
        """
        Add the primary field to the facet machine. This field is tesselated
        into NFacets by setFacetsLocs method
        Args:
            Npix:
            Cell:
            NFacets:
            Support:
            OverS:
            Padding:
            wmax:
            Nw:
            RaDecRad:
            ImageName:
            **kw:
        """
        Cell = self.GD["Image"]["Cell"]

        self.ImageName = ImageName

        self.LraFacet = []
        self.LdecFacet = []

        self.ChanFreq = self.VS.GlobalFreqs

        self.NFacets = NFacets
        self.Cell = Cell
        self.CellSizeRad = (Cell / 3600.) * np.pi / 180.
        rac, decc = self.VS.CurrentMS.radec
        self.MainRaDec = (rac, decc)
        self.nch = self.VS.NFreqBands
        self.NChanGrid = self.nch
        self.SumWeights = np.zeros((self.NChanGrid, self.npol), float)

        self.CoordMachine = ModCoord.ClassCoordConv(rac, decc)
        # get the closest fast fft size:
        Npix = self.GD["Image"]["NPix"]
        Padding = self.GD["RIME"]["Padding"]
        self.Padding = Padding
        Npix, _ = EstimateNpix(float(Npix), Padding=1)
        self.Npix = Npix
        self.OutImShape = (self.nch, self.npol, self.Npix, self.Npix)
        # image bounding box in radians:
        RadiusTot = self.CellSizeRad * self.Npix / 2
        self.RadiusTot = RadiusTot
        self.CornersImageTot = np.array([[-RadiusTot, -RadiusTot],
                                         [RadiusTot, -RadiusTot],
                                         [RadiusTot, RadiusTot],
                                         [-RadiusTot, RadiusTot]])
        self.setFacetsLocs()

    def AppendFacet(self, iFacet, l0, m0, diam):
        """
        Adds facet dimentions to info dict of facets (self.DicoImager[iFacet])
        Args:
            iFacet:
            l0:
            m0:
            diam:
        """
        diam *= self.Oversize

        DicoConfigGM = None
        lmShift = (l0, m0)
        self.DicoImager[iFacet]["lmShift"] = lmShift
        # CellRad=(Cell/3600.)*np.pi/180.

        raFacet, decFacet = self.CoordMachine.lm2radec(
                            np.array([lmShift[0]]), np.array([lmShift[1]]))

        NpixFacet, _ = EstimateNpix(diam / self.CellSizeRad, Padding=1)
        _, NpixPaddedGrid = EstimateNpix(NpixFacet, Padding=self.Padding)

        diam = NpixFacet * self.CellSizeRad
        diamPadded = NpixPaddedGrid * self.CellSizeRad
        RadiusFacet = diam * 0.5
        RadiusFacetPadded = diamPadded * 0.5
        self.DicoImager[iFacet]["lmDiam"] = RadiusFacet
        self.DicoImager[iFacet]["lmDiamPadded"] = RadiusFacetPadded
        self.DicoImager[iFacet]["RadiusFacet"] = RadiusFacet
        self.DicoImager[iFacet]["RadiusFacetPadded"] = RadiusFacetPadded
        self.DicoImager[iFacet]["lmExtent"] = l0 - RadiusFacet, \
            l0 + RadiusFacet, m0 - RadiusFacet, m0 + RadiusFacet
        self.DicoImager[iFacet]["lmExtentPadded"] = l0 - RadiusFacetPadded, \
            l0 + RadiusFacetPadded, \
            m0 - RadiusFacetPadded, \
            m0 + RadiusFacetPadded

        lSol, mSol = self.lmSols
        raSol, decSol = self.radecSols
        dSol = np.sqrt((l0 - lSol) ** 2 + (m0 - mSol) ** 2)
        iSol = np.where(dSol == np.min(dSol))[0]
        self.DicoImager[iFacet]["lmSol"] = lSol[iSol], mSol[iSol]
        self.DicoImager[iFacet]["radecSol"] = raSol[iSol], decSol[iSol]
        self.DicoImager[iFacet]["iSol"] = iSol

        # print>>log,"#[%3.3i] %f, %f"%(iFacet,l0,m0)
        DicoConfigGM = {"NPix": NpixFacet,
                        "Cell": self.GD["Image"]["Cell"],
                        "ChanFreq": self.ChanFreq,
                        "DoPSF": False,
                        "Support": self.GD["CF"]["Support"],
                        "OverS": self.GD["CF"]["OverS"],
                        "wmax": self.GD["CF"]["wmax"],
                        "Nw": self.GD["CF"]["Nw"],
                        "WProj": True,
                        "DoDDE": self.DoDDE,
                        "Padding": self.GD["RIME"]["Padding"]}

        _, _, NpixOutIm, NpixOutIm = self.OutImShape

        self.DicoImager[iFacet]["l0m0"] = lmShift
        self.DicoImager[iFacet]["RaDec"] = raFacet[0], decFacet[0]
        self.LraFacet.append(raFacet[0])
        self.LdecFacet.append(decFacet[0])
        xc, yc = int(round(l0 / self.CellSizeRad + NpixOutIm / 2)), \
            int(round(m0 / self.CellSizeRad + NpixOutIm / 2))

        self.DicoImager[iFacet]["pixCentral"] = xc, yc
        self.DicoImager[iFacet]["pixExtent"] = round(xc - NpixFacet / 2), \
            round(xc + NpixFacet / 2 + 1), \
            round(yc - NpixFacet / 2), \
            round(yc + NpixFacet / 2 + 1)

        self.DicoImager[iFacet]["NpixFacet"] = NpixFacet
        self.DicoImager[iFacet]["NpixFacetPadded"] = NpixPaddedGrid
        self.DicoImager[iFacet]["DicoConfigGM"] = DicoConfigGM
        self.DicoImager[iFacet]["IDFacet"] = iFacet
        # print self.DicoImager[iFacet]

        self.FacetCat.ra[iFacet] = raFacet[0]
        self.FacetCat.dec[iFacet] = decFacet[0]
        l, m = self.DicoImager[iFacet]["l0m0"]
        self.FacetCat.l[iFacet] = l
        self.FacetCat.m[iFacet] = m
        self.FacetCat.Cluster[iFacet] = iFacet

    def setFacetsLocs(self):
        """
        Routine to split the image into a grid of squares.
        This can be overridden to perform more complex tesselations
        """
        Npix = self.GD["Image"]["NPix"]
        NFacets = self.GD["Facets"]["NFacets"]
        Padding = self.GD["RIME"]["Padding"]
        self.Padding = Padding
        NpixFacet, _ = EstimateNpix(float(Npix) / NFacets, Padding=1)
        Npix = NpixFacet * NFacets
        self.Npix = Npix
        self.OutImShape = (self.nch, self.npol, self.Npix, self.Npix)
        _, NpixPaddedGrid = EstimateNpix(NpixFacet, Padding=Padding)
        self.NpixPaddedFacet = NpixPaddedGrid
        self.NpixFacet = NpixFacet
        self.FacetShape = (self.nch, self.npol, NpixFacet, NpixFacet)
        self.PaddedGridShape = (self.NChanGrid, self.npol,
                                NpixPaddedGrid, NpixPaddedGrid)

        RadiusTot = self.CellSizeRad * self.Npix / 2
        self.RadiusTot = RadiusTot

        lMainCenter, mMainCenter = 0., 0.
        self.lmMainCenter = lMainCenter, mMainCenter
        self.CornersImageTot = np.array(
                                [[lMainCenter - RadiusTot, mMainCenter - RadiusTot],
                                 [lMainCenter + RadiusTot, mMainCenter - RadiusTot],
                                 [lMainCenter + RadiusTot, mMainCenter + RadiusTot],
                                 [lMainCenter - RadiusTot, mMainCenter + RadiusTot]])

        print>> log, "Sizes (%i x %i facets):" % (NFacets, NFacets)
        print>> log, "   - Main field :   [%i x %i] pix" % \
            (self.Npix, self.Npix)
        print>> log, "   - Each facet :   [%i x %i] pix" % \
            (NpixFacet, NpixFacet)
        print>> log, "   - Padded-facet : [%i x %i] pix" % \
            (NpixPaddedGrid, NpixPaddedGrid)

        ############################

        self.NFacets = NFacets
        lrad = Npix * self.CellSizeRad * 0.5
        self.ImageExtent = [-lrad, lrad, -lrad, lrad]

        lfacet = NpixFacet * self.CellSizeRad * 0.5
        lcenter_max = lrad - lfacet
        lFacet, mFacet, = np.mgrid[-lcenter_max:lcenter_max:(NFacets) * 1j,
                                   -lcenter_max:lcenter_max:(NFacets) * 1j]
        lFacet = lFacet.flatten()
        mFacet = mFacet.flatten()
        x0facet, y0facet = np.mgrid[0:Npix:NpixFacet, 0:Npix:NpixFacet]
        x0facet = x0facet.flatten()
        y0facet = y0facet.flatten()

        # print "Append1"; self.IM.CI.E.clear()

        self.DicoImager = {}
        for iFacet in xrange(lFacet.size):
            self.DicoImager[iFacet] = {}

        # print "Append2"; self.IM.CI.E.clear()

        self.FacetCat = np.zeros(
            (lFacet.size,),
            dtype=[('Name', '|S200'),
                   ('ra', np.float),
                   ('dec', np.float),
                   ('SumI', np.float),
                   ("Cluster", int),
                   ("l", np.float),
                   ("m", np.float),
                   ("I", np.float)])

        self.FacetCat = self.FacetCat.view(np.recarray)
        self.FacetCat.I = 1
        self.FacetCat.SumI = 1

        for iFacet in xrange(lFacet.size):
            l0 = x0facet[iFacet] * self.CellSizeRad
            m0 = y0facet[iFacet] * self.CellSizeRad
            l0 = lFacet[iFacet]
            m0 = mFacet[iFacet]

            # print x0facet[iFacet],y0facet[iFacet],l0,m0
            self.AppendFacet(iFacet, l0, m0, NpixFacet * self.CellSizeRad)


        
        #self.iCentralFacet = self.DicoImager[lFacet.size / 2]

        self.SetLogModeSubModules("Silent")
        self.MakeREG()

    def MakeREG(self):
        """
        Writes out ds9 tesselation region file
        """
        regFile = "%s.Facets.reg" % self.ImageName

        print>>log, "Writing facets locations in %s" % regFile

        f = open(regFile, "w")
        f.write("# Region file format: DS9 version 4.1\n")
        ss0 = 'global color=green dashlist=8 3 width=1 font="helvetica 10 \
            normal roman" select=1 highlite=1 dash=0'
        ss1 = ' fixed=0 edit=1 move=1 delete=1 include=1 source=1\n'

        f.write(ss0+ss1)
        f.write("fk5\n")

        for iFacet in self.DicoImager.keys():
            # rac,decc=self.DicoImager[iFacet]["RaDec"]
            l0, m0 = self.DicoImager[iFacet]["l0m0"]
            diam = self.DicoImager[iFacet]["lmDiam"]
            dl = np.array([-1, 1, 1, -1, -1])*diam
            dm = np.array([-1, -1, 1, 1, -1])*diam
            l = ((dl.flatten()+l0)).tolist()
            m = ((dm.flatten()+m0)).tolist()

            x = []
            y = []
            for iPoint in xrange(len(l)):
                xp, yp = self.CoordMachine.lm2radec(np.array(
                    [l[iPoint]]), np.array([m[iPoint]]))
                x.append(xp)
                y.append(yp)

            x = np.array(x)  # +[x[2]])
            y = np.array(y)  # +[y[2]])

            x *= 180/np.pi
            y *= 180/np.pi

            for iline in xrange(x.shape[0]-1):
                x0 = x[iline]
                y0 = y[iline]
                x1 = x[iline+1]
                y1 = y[iline+1]
                f.write("line(%f,%f,%f,%f) # line=0 0\n" % (x0, y0, x1, y1))

        f.close()

    # ############### Initialisation #####################

    def PlotFacetSols(self):

        DicoClusterDirs= NpShared.SharedToDico("%sDicoClusterDirs" % self.IdSharedMemData)
        lc=DicoClusterDirs["l"]
        mc=DicoClusterDirs["m"]
        sI=DicoClusterDirs["I"]
        x0,x1=lc.min()-np.pi/180,lc.max()+np.pi/180
        y0,y1=mc.min()-np.pi/180,mc.max()+np.pi/180
        InterpMode=self.GD["DDESolutions"]["Type"]
        if InterpMode=="Krigging":
            import pylab
            for iFacet in sorted(self.DicoImager.keys()):
                l0, m0 = self.DicoImager[iFacet]["lmShift"]
                d0 = self.GD["DDESolutions"]["Scale"]*np.pi/180
                gamma = self.GD["DDESolutions"]["gamma"]

                d = np.sqrt((l0-lc)**2+(m0-mc)**2)
                idir = np.argmin(d)  # this is not used
                w = sI/(1.+d/d0) ** gamma
                w /= np.sum(w)
                w[w < (0.2 * w.max())] = 0
                ind = np.argsort(w)[::-1]
                w[ind[4::]] = 0

                ind = np.where(w != 0)[0]
                pylab.clf()
                pylab.scatter(lc[ind], mc[ind], c=w[ind], vmin=0, vmax=w.max())
                pylab.scatter([l0], [m0], marker="+")
                pylab.xlim(x0, x1)
                pylab.ylim(y0, y1)
                pylab.draw()
                pylab.show(False)
                pylab.pause(0.1)

    def Init(self):
        """
        Initialize either in parallel or serial
        """
        self.DicoGridMachine = {}
        for iFacet in self.DicoImager.keys():
            self.DicoGridMachine[iFacet] = {}
        self.setWisdom()
        self._CF = None
        self.IsDDEGridMachineInit = False
        self.SetLogModeSubModules("Loud")
        self._Im2Grid = ClassImToGrid(OverS=self.GD["CF"]["OverS"], GD=self.GD)


    def setWisdom(self):
        """
        Set fft wisdom
        """
        cachename = "FFTW_Wisdom_PSF" if self.DoPSF and self.Oversize != 1 else "FFTW_Wisdom"
        path, valid = self.VS.maincache.checkCache(cachename, dict(shape=self.PaddedGridShape))
        if not valid:
            print>>log, "Computing fftw wisdom for shape = %s" % str(self.PaddedGridShape)
            a = np.random.randn(*(self.PaddedGridShape)) \
                + 1j*np.random.randn(*(self.PaddedGridShape))
            FM = ModFFTW.FFTW_2Donly(self.PaddedGridShape, np.complex64)
            FM.fft(a)  # this is never used -- only to compute the wisdom
            self.FFTW_Wisdom = pyfftw.export_wisdom()
            cPickle.dump(self.FFTW_Wisdom, file(path, "w"))
            self.VS.maincache.saveCache(cachename)
        else:
            print>>log, "Loading cached fftw wisdom from %s" % path
            self.FFTW_Wisdom = cPickle.load(file(path))
            # this is inherited by forked processes, presumably
            pyfftw.import_wisdom(self.FFTW_Wisdom)

            # for iFacet in sorted(self.DicoImager.keys()):
        #     A = ModFFTW.GiveFFTW_aligned(self.PaddedGridShape, np.complex64)
        #     NpShared.ToShared("%sFFTW.%i" % (self.IdSharedMem, iFacet), A)

    def initCFInBackground (self, other_fm=None):
        # if we have another FacetMachine supplied, check if the same CFs apply
        if other_fm and self.Oversize == other_fm.Oversize:
            self._CF = other_fm._CF
            self.IsDDEGridMachineInit = True
            return
        # subprocesses will place W-terms etc. here. Reset this first.
        self._CF = SharedDict.create("CFPSF" if self.DoPSF else "CF")
        # check if w-kernels, spacial weights, etc. are cached
        cachekey = dict(ImagerCF=self.GD["CF"], ImagerMainFacet=self.GD["Image"])
        cachename = self._cf_cachename = "CF"
        # in oversize-PSF mode, make separate cache for PSFs
        if self.DoPSF and self.Oversize != 1:
            cachename = self._cf_cachename = "CFPSF"
            cachekey["Oversize"] = self.Oversize
        # check cache
        cachepath, cachevalid = self.VS.maincache.checkCache(cachename, cachekey, directory=True)
        # up to workers to load/save cache
        for iFacet in self.DicoImager.iterkeys():
            APP.runJob("%s.InitCF.f%s"%(self._app_id, iFacet), self._initcf_worker,
                            args=(iFacet, self._CF.path, cachepath, cachevalid))

    def awaitInitCompletion (self):
        if not self.IsDDEGridMachineInit:
            APP.awaitJobResults("%s.InitCF.*"%self._app_id, progress="Init CFs")
            self._CF.reload()
            # mark cache as safe
            self.VS.maincache.saveCache(self._cf_cachename)
            self.IsDDEGridMachineInit = True

    def _createGridMachine(self, iFacet, **kw):
        """Helper method for workers: creates a GridMachine with the given extra keyword arguments"""
        FacetInfo = self.DicoImager[iFacet]
        return ClassDDEGridMachine.ClassDDEGridMachine(
            self.GD,
            FacetInfo["DicoConfigGM"]["ChanFreq"],
            FacetInfo["DicoConfigGM"]["NPix"],
            FacetInfo["lmShift"],
            iFacet, self.SpheNorm, self.VS.NFreqBands,
            self.VS.StokesConverter.AvailableCorrelationProductsIds(),
            self.VS.StokesConverter.RequiredStokesProductsIds(),
            **kw)

    def _initcf_worker (self, iFacet, cfdict_path, cachepath, cachevalid):
        """Worker method of InitParal"""
        path = "%s/%s.npz" % (cachepath, iFacet)
        if self._CF is None or self._CF.path != cfdict_path:
            self._CF = SharedDict.attach(cfdict_path, load=False)
        facet_dict = self._CF.addSubdict(iFacet)
        # try to load the cache, and copy it to the shared facet dict
        if cachevalid:
            try:
                npzfile = np.load(file(path))
                for key, value in npzfile.iteritems():
                    facet_dict[key] = value
                # validate dict
                ClassDDEGridMachine.ClassDDEGridMachine.verifyCFDict(facet_dict, self.GD["CF"]["Nw"])
            except:
                print>>log,traceback.format_exc()
                print>>log, "Error loading %s, will re-generate"%path
        # ok, regenerate the terms at this point
        FacetInfo = self.DicoImager[iFacet]
        # Create smoothned facet tessel mask:
        Npix = FacetInfo["NpixFacetPadded"]
        l0, l1, m0, m1 = FacetInfo["lmExtentPadded"]
        X, Y = np.mgrid[l0:l1:Npix * 1j, m0:m1:Npix * 1j]
        XY = np.dstack((X, Y))
        XY_flat = XY.reshape((-1, 2))
        vertices = FacetInfo["Polygon"]
        mpath = Path(vertices)  # the vertices of the polygon
        mask_flat = mpath.contains_points(XY_flat)
        mask = mask_flat.reshape(X.shape)
        mpath = Path(self.CornersImageTot)
        mask_flat2 = mpath.contains_points(XY_flat)
        mask2 = mask_flat2.reshape(X.shape)
        mask[mask2 == 0] = 0

        GaussPars = (10, 10, 0)

        # compute spatial weight term
        sw = np.float32(mask.reshape((1, 1, Npix, Npix)))
        sw = ModFFTW.ConvolveGaussian(sw, CellSizeRad=1, GaussPars=[GaussPars])
        sw = sw.reshape((Npix, Npix))
        sw /= np.max(sw)
        facet_dict["SW"] = sw

        # Initialize a grid machine per iFacet, this will implicitly compute wterm and Sphe
        self._createGridMachine(iFacet, cf_dict=facet_dict, compute_cf=True)

        # save cache
        np.savez(file(path, "w"), **facet_dict)
        return "compute"


    def setCasaImage(self, ImageName=None, Shape=None, Freqs=None, Stokes=["I"]):
        if ImageName is None:
            ImageName = self.ImageName

        if Shape is None:
            Shape = self.OutImShape
        self.CasaImage = ClassCasaImage.ClassCasaimage(
            ImageName, Shape, self.Cell, self.MainRaDec, Freqs=Freqs, Stokes=Stokes)

    def ToCasaImage(self, ImageIn, Fits=True, ImageName=None,
                    beam=None, beamcube=None, Freqs=None, Stokes=["I"]):
        self.setCasaImage(ImageName=ImageName, Shape=ImageIn.shape,
                          Freqs=Freqs, Stokes=Stokes)

        self.CasaImage.setdata(ImageIn, CorrT=True)

        if Fits:
            self.CasaImage.ToFits()
            if beam is not None:
                self.CasaImage.setBeam(beam, beamcube=beamcube)
        self.CasaImage.close()
        self.CasaImage = None

    def GiveEmptyMainField(self):
        """
        Gives empty image of the correct shape to act as buffer for e.g. the stitching process
        Returns:
            ndarray of type complex
        """
        return np.zeros(self.OutImShape, dtype=self.stitchedType)

    def putChunkInBackground(self, DATA):
        """
        """
        self.SetLogModeSubModules("Silent")
        if not self.IsDirtyInit:
            self.ReinitDirty()
        self.gridChunkInBackground(DATA)
        self.SetLogModeSubModules("Loud")

    def getChunkInBackground(self, DATA):
        """Gets visibilities corresponding to current model image."""
        if self.DoPSF:
            raise RuntimeError("Can't call getChunk on a PSF mode FacetMachine. This is a bug!")
        self.SetLogModeSubModules("Silent")
        self.degridChunkInBackground(DATA)
        self.SetLogModeSubModules("Loud")

    def ComputeSmoothBeam(self):
        if self.DoComputeSmoothBeam and self.GD["Beam"]["BeamModel"]!=None and self.SmoothMeanNormImage is None:
            _,npol,Npix,Npix=self.OutImShape
            self.AverageBeamMachine=ClassBeamMean.ClassBeamMean(self.VS)
            self.AverageBeamMachine.CalcMeanBeam()
            self.SmoothMeanNormImage = self.AverageBeamMachine.SmoothBeam.reshape((1,1,Npix,Npix))
            #self.AverageBeamMachine.GiveMergedWithDiscrete( np.mean(self.NormData, axis=0).reshape((Npix,Npix) ))
            #self.SmoothMeanNormImage = self.SmoothMeanNormImage.reshape((1,1,Npix,Npix))
            #DicoImages["SmoothMeanNormImage"] = self.SmoothMeanNormImage 

    def setModelImage(self, ModelImage):
        """Sets current model image. Copies it to a shared dict and returns shared array version of image."""
        if self.DoPSF:
            raise RuntimeError("Can't call getChunk on a PSF mode FacetMachine. This is a bug!")
        self._model_dict = SharedDict.create("Model")
        self._model_dict["Id"] = id(ModelImage), id(self._model_dict)
        self._model_dict["Image"] = ModelImage
        return self._model_dict["Image"]

    def releaseModelImage(self):
        """Deletes current model image from SHM. USe to save RAM."""
        if self._model_dict is not None:
            self._model_dict.delete()
            self._model_dict = None

    def FacetsToIm(self, NormJones=False):
        """
        Fourier transforms the individual facet grids and then
        Stitches the gridded facets and builds the following maps:
            self.stitchedResidual (initial residual is the dirty map)
            self.NormImage (grid-correcting map, see also: BuildFacetNormImage() method)
            self.MeanResidual ("average" residual map taken over all continuum bands of the residual cube,
                               this will be the same as stitchedResidual if there is only one continuum band in the residual
                               cube)
            self.DicoPSF if the facet machine is set to produce a PSF. This contains, amongst others a PSF and mean psf per facet
            Note that only the stitched residuals are currently normalized and converted to stokes images for cleaning.
            This is because the coplanar facets should be jointly cleaned on a single map.
        Args:
            NormJones: if True (and there is Jones Norm data available) also computes self.NormData (ndarray) of jones
            averages.
            psf: if True (and PSF grids are available), also computes PSF terms


        Returns:
            Dictionary containing:
            "ImagData" = self.stitchedResidual
            "NormImage" = self.NormImage (grid-correcting map)
            "NormData" = self.NormData (if computed, see above)
            "MeanImage" = self.MeanResidual
            "freqs" = channel information on the bands being averaged into each of the continuum slices of the residual
            "SumWeights" = sum of visibility weights used in normalizing the gridded correlations
            "WeightChansImages" = normalized weights
        """
        # wait for any outstanding grid jobs to finish
        self.collectGriddingResults()

        if not self.HasFourierTransformed:
            self.fourierTransformInBackground()
            self.HasFourierTransformed = True
        _, npol, Npix, Npix = self.OutImShape
        DicoImages = {}
        DicoImages["freqs"] = {}

        DoCalcNormData = NormJones and self.NormData is None

        # Assume all facets have the same weight sums.
        # Store the normalization weights for reference
        DicoImages["SumWeights"] = np.zeros((self.VS.NFreqBands, self.npol), np.float64)
        for band, channels in enumerate(self.VS.FreqBandChannels):
            DicoImages["freqs"][band] = channels
            DicoImages["SumWeights"][band] = self.DicoImager[0]["SumWeights"][band]
        DicoImages["WeightChansImages"] = DicoImages["SumWeights"] / np.sum(DicoImages["SumWeights"])

        # compute sum of Jones terms per facet and channel
        for iFacet in self.DicoImager.keys():
            self.DicoImager[iFacet]["SumJonesNorm"] = np.zeros(self.VS.NFreqBands, np.float64)
            for Channel in xrange(self.VS.NFreqBands):
                ThisSumSqWeights = self.DicoImager[iFacet]["SumJones"][1][Channel]
                if ThisSumSqWeights == 0:
                    ThisSumSqWeights = 1.
                ThisSumJones = self.DicoImager[iFacet]["SumJones"][0][Channel] / ThisSumSqWeights
                if ThisSumJones == 0:
                    ThisSumJones = 1.
                self.DicoImager[iFacet]["SumJonesNorm"][Channel] = ThisSumJones

        # build facet-normalization image
        if self.NormImage is None:
            self.NormImage = self.BuildFacetNormImage()
            self.NormImageReShape = self.NormImage.reshape([1, 1, self.NormImage.shape[0], self.NormImage.shape[1]])

        self.stitchedResidual = self.FacetsToIm_Channel()

        # build Jones amplitude image
        if DoCalcNormData:
            self.NormData = self.FacetsToIm_Channel("Jones-amplitude")

        # compute normalized per-band weights (WBAND)
        if self.VS.MultiFreqMode:
            WBAND = np.array([DicoImages["SumWeights"][Channel] for Channel in xrange(self.VS.NFreqBands)])
            # sum frequency contribution to weights per correlation
            WBAND /= np.sum(WBAND, axis=0)
            WBAND = np.float32(WBAND.reshape((self.VS.NFreqBands, npol, 1, 1)))
        else:
            WBAND = 1
        #  ok, make sure the FTs have been computed
        self.collectFourierTransformResults()
        # PSF mode: construct PSFs
        if self.DoPSF:
            self.DicoPSF = {}
            print>>log, "building PSF facet-slices"
            for iFacet in self.DicoGridMachine.keys():
                # first normalize by spheroidals - these
                # facet psfs will be used in deconvolution per facet
                SPhe = self._CF[iFacet]["Sphe"]
                nx = SPhe.shape[0]
                SPhe = SPhe.reshape((1, 1, nx, nx)).real
                self.DicoPSF[iFacet] = {}
                self.DicoPSF[iFacet]["PSF"] = self._facet_grids[iFacet].real.copy()
                self.DicoPSF[iFacet]["PSF"] /= SPhe
                #self.DicoPSF[iFacet]["PSF"][SPhe < 1e-2] = 0
                self.DicoPSF[iFacet]["l0m0"] = self.DicoImager[iFacet]["l0m0"]
                self.DicoPSF[iFacet]["pixCentral"] = self.DicoImager[iFacet]["pixCentral"]
                self.DicoPSF[iFacet]["lmSol"] = self.DicoImager[iFacet]["lmSol"]

                nch, npol, n, n = self.DicoPSF[iFacet]["PSF"].shape
                PSFChannel = np.zeros((nch, npol, n, n), self.stitchedType)
                for ch in xrange(nch):
                    self.DicoPSF[iFacet]["PSF"][ch][SPhe[0] < 1e-2] = 0
                    self.DicoPSF[iFacet]["PSF"][ch][0] = self.DicoPSF[iFacet]["PSF"][ch][0].T[::-1, :]
                    SumJonesNorm = self.DicoImager[iFacet]["SumJonesNorm"][ch]
                    # normalize to bring back transfer
                    # functions to approximate convolution
                    self.DicoPSF[iFacet]["PSF"][ch] /= np.sqrt(SumJonesNorm)
                    for pol in xrange(npol):
                        ThisSumWeights = self.DicoImager[iFacet]["SumWeights"][ch][pol]
                        # normalize the response per facet
                        # channel if jones corrections are enabled
                        self.DicoPSF[iFacet]["PSF"][ch][pol] /= ThisSumWeights
                    PSFChannel[ch, :, :, :] = self.DicoPSF[iFacet]["PSF"][ch][:, :, :]

                W = DicoImages["WeightChansImages"]
                W = np.float32(W.reshape((self.VS.NFreqBands, npol, 1, 1)))
                # weight each of the cube slices and average
                MeanPSF = np.sum(PSFChannel * W, axis=0).reshape((1, npol, n, n))
                self.DicoPSF[iFacet]["MeanPSF"] = MeanPSF

            DicoVariablePSF = self.DicoPSF
            NFacets = len(DicoVariablePSF.keys())

            if self.GD["RIME"]["Circumcision"]:
                NPixMin = self.GD["RIME"]["Circumcision"]
                # print>>log,"using explicit Circumcision=%d"%NPixMin
            else:
                NPixMin = 1e6
                for iFacet in sorted(DicoVariablePSF.keys()):
                    _, npol, n, n = DicoVariablePSF[iFacet]["PSF"].shape
                    if n < NPixMin:
                        NPixMin = n

                NPixMin = int(NPixMin/self.GD["RIME"]["Padding"])
                if not NPixMin % 2:
                    NPixMin += 1
                    # print>>log,"using computed Circumcision=%d"%NPixMin

            nch = self.VS.NFreqBands
            CubeVariablePSF = np.zeros((NFacets, nch, npol, NPixMin, NPixMin), np.float32)
            CubeMeanVariablePSF = np.zeros((NFacets, 1, npol, NPixMin, NPixMin), np.float32)

            print>>log, "cutting PSF facet-slices of shape %dx%d" % (NPixMin, NPixMin)
            for iFacet in sorted(DicoVariablePSF.keys()):
                _, npol, n, n = DicoVariablePSF[iFacet]["PSF"].shape
                for ch in xrange(nch):
                    i = n/2 - NPixMin/2
                    j = n/2 + NPixMin/2 + 1
                    CubeVariablePSF[iFacet, ch, :, :, :] = DicoVariablePSF[iFacet]["PSF"][ch][:, i:j, i:j]
                CubeMeanVariablePSF[iFacet, 0, :, :, :] = DicoVariablePSF[iFacet]["MeanPSF"][0, :, i:j, i:j]

            self.DicoPSF["CentralFacet"] = self.iCentralFacet
            self.DicoPSF["CubeVariablePSF"] = CubeVariablePSF
            self.DicoPSF["CubeMeanVariablePSF"] = CubeMeanVariablePSF
            self.DicoPSF["MeanFacetPSF"] = np.mean(CubeMeanVariablePSF, axis=0).reshape((1, npol, NPixMin, NPixMin))
            self.DicoPSF["MeanJonesBand"] = []

            print>>log,"  Building Facets-PSF normalised by their maximum"
            self.DicoPSF["PeakNormed_CubeMeanVariablePSF"]=np.zeros_like(self.DicoPSF["CubeMeanVariablePSF"])
            self.DicoPSF["PeakNormed_CubeVariablePSF"]=np.zeros_like(self.DicoPSF["CubeVariablePSF"])
            for iFacet in sorted(self.DicoImager.keys()):
                self.DicoPSF["PeakNormed_CubeMeanVariablePSF"][iFacet]=CubeMeanVariablePSF[iFacet]/np.max(CubeMeanVariablePSF[iFacet])
                for iChan in range(nch):
                    self.DicoPSF["PeakNormed_CubeVariablePSF"][iFacet,iChan]=CubeVariablePSF[iFacet,iChan]/np.max(CubeVariablePSF[iFacet,iChan])

            PeakFacet=np.max(np.max(np.max(CubeMeanVariablePSF,axis=-1),axis=-1),axis=-1).reshape((NFacets,1,1,1,1))
            PeakNormed_CubeMeanVariablePSF=CubeMeanVariablePSF/PeakFacet
            #self.DicoPSF["MeanFacetPSF"]=np.mean(CubeMeanVariablePSF,axis=0).reshape((1,npol,NPixMin,NPixMin))
            self.DicoPSF["MeanFacetPSF"]=np.mean(PeakNormed_CubeMeanVariablePSF,axis=0).reshape((1,npol,NPixMin,NPixMin))
            self.DicoPSF["MeanJonesBand"]=[]

            self.DicoPSF["OutImShape"] = self.OutImShape
            self.DicoPSF["CellSizeRad"] = self.CellSizeRad
            for iFacet in sorted(self.DicoImager.keys()):
                MeanJonesBand = np.zeros((self.VS.NFreqBands,), np.float64)
                for Channel in xrange(self.VS.NFreqBands):
                    ThisSumSqWeights = self.DicoImager[iFacet]["SumJones"][1][Channel] or 1
                    ThisSumJones = (self.DicoImager[iFacet]["SumJones"][0][Channel] / ThisSumSqWeights) or 1
                    MeanJonesBand[Channel] = ThisSumJones
                self.DicoPSF["MeanJonesBand"].append(MeanJonesBand)

            self.DicoPSF["SumJonesChan"] = []
            self.DicoPSF["SumJonesChanWeightSq"] = []
            for iFacet in sorted(self.DicoImager.keys()):
                ThisFacetSumJonesChan = []
                ThisFacetSumJonesChanWeightSq = []
                for iMS in xrange(self.VS.nMS):
                    A = self.DicoImager[iFacet]["SumJonesChan"][iMS][1, :]
                    A[A == 0] = 1.
                    A = self.DicoImager[iFacet]["SumJonesChan"][iMS][0, :]
                    A[A == 0] = 1.
                    SumJonesChan = self.DicoImager[iFacet]["SumJonesChan"][iMS][0, :]
                    SumJonesChanWeightSq = self.DicoImager[iFacet]["SumJonesChan"][iMS][1, :]
                    ThisFacetSumJonesChan.append(SumJonesChan)
                    ThisFacetSumJonesChanWeightSq.append(SumJonesChanWeightSq)

                self.DicoPSF["SumJonesChan"].append(ThisFacetSumJonesChan)
                self.DicoPSF["SumJonesChanWeightSq"].append(ThisFacetSumJonesChanWeightSq)
            self.DicoPSF["ChanMappingGrid"] = self.VS.DicoMSChanMapping
            self.DicoPSF["ChanMappingGridChan"] = self.VS.DicoMSChanMappingChan
            self.DicoPSF["freqs"] = DicoImages["freqs"]
            self.DicoPSF["WeightChansImages"] = DicoImages["WeightChansImages"]

            self.DicoPSF["ImagData"] = self.FacetsToIm_Channel("PSF")
            if self.VS.MultiFreqMode:
                self.DicoPSF["MeanImage"] = np.sum(self.DicoPSF["ImagData"] * WBAND, axis=0).reshape((1, npol, Npix, Npix))
            else:
                self.DicoPSF["MeanImage"] = self.DicoPSF["ImagData"]

            self.DicoPSF["NormImage"] = self.NormImage
            self._psf_dict = self.DicoPSF = SharedDict.dict_to_shm("dictPSF",self.DicoPSF)

            return self.DicoPSF

        # else build Dirty (residual) image
        else:
            # Build a residual image consisting of multiple continuum bands
            self.stitchedResidual = self.FacetsToIm_Channel("Dirty")
            if self.VS.MultiFreqMode:
                self.MeanResidual = np.sum(self.stitchedResidual * WBAND, axis=0).reshape((1, npol, Npix, Npix))
            else:
                ### (Oleg 24/12/2016: removed the .copy(), why was this needed? Note that in e.g.
                ### ClassImageDeconvMachineMSMF.SubStep(), there is an if-clause such as
                ###    "if self._MeanDirty is not self._CubeDirty: do_expensive_operation"
                ### which the .copy() operation here defeats, so I remove it
                self.MeanResidual = self.stitchedResidual  #.copy()
            DicoImages["ImagData"] = self.stitchedResidual
            DicoImages["NormImage"] = self.NormImage  # grid-correcting map
            DicoImages["NormData"] = self.NormData
            DicoImages["MeanImage"] = self.MeanResidual
            return DicoImages

    def BuildFacetNormImage(self):
        """
        Creates a stitched tesselation weighting map. This can be useful
        to downweight areas where facets overlap (e.g. padded areas)
        before stitching the facets into one map.
        Returns
            ndarray with norm image
        """
        print>>log, "  Building Facet-normalisation image"
        nch, npol = self.nch, self.npol
        _, _, NPixOut, NPixOut = self.OutImShape
        # in PSF mode, make the norm image in memory. In normal mode, make it in the shared dict,
        # since the degridding workers require it
        NormImage = np.zeros((NPixOut, NPixOut), dtype=self.stitchedType)
        for iFacet in self.DicoImager.keys():
            xc, yc = self.DicoImager[iFacet]["pixCentral"]
            NpixFacet = self.DicoImager[iFacet]["NpixFacetPadded"]

            Aedge, Bedge = GiveEdges((xc, yc), NPixOut,
                                     (NpixFacet/2, NpixFacet/2), NpixFacet)
            x0d, x1d, y0d, y1d = Aedge
            x0p, x1p, y0p, y1p = Bedge

            SpacialWeigth = self._CF[iFacet]["SW"].T[::-1, :]
            SW = SpacialWeigth[::-1, :].T[x0p:x1p, y0p:y1p]
            NormImage[x0d:x1d, y0d:y1d] += np.real(SW)

        self.NormImage = NormImage
        #self.NormImage = NpShared.ToShared("%sNormImage"%self.IdSharedMem,self.NormImage)
        self.NormImageReShape = self.NormImage.reshape([1,1,
                                                        self.NormImage.shape[0],
                                                        self.NormImage.shape[1]])
        return NormImage

    def FacetsToIm_Channel(self, kind="Dirty"):
        """
        Preconditions: assumes the stitched tesselation weighting map has been
        created previously
        Args:
            kind: one of "Jones-amplitude", "Dirty", or "PSF", to create a stitched Jones amplitude, dirty or psf image
        Returns:
            Image cube, which may contain multiple correlations
            and continuum channel bands
        """
        T = ClassTimeIt.ClassTimeIt("FacetsToIm_Channel")
        T.disable()
        Image = self.GiveEmptyMainField()

        nch, npol, NPixOut, NPixOut = self.OutImShape

        print>>log, "Combining facets to stitched %s image" % kind

        for Channel in range(self.VS.NFreqBands):
            ThisSumWeights=self.DicoImager[0]["SumWeights"][Channel][0]
            if ThisSumWeights==0:
                print>>log,ModColor.Str("The sum of the weights are zero for FreqBand #%i, data is all flagged?"%Channel)
                print>>log,ModColor.Str("  (... will skip normalisation for this FreqBand)")
                
        pBAR = ProgressBar('white', width=50, block='=', empty=' ', Title="Gluing facets", HeaderSize=10, TitleSize=13)
        NFacets=len(self.DicoImager.keys())
        pBAR.render(0, '%4i/%i' % (0, NFacets))

        for iFacet in self.DicoImager.keys():

            SPhe = self._CF[iFacet]["Sphe"]
            SpacialWeigth = self._CF[iFacet]["SW"].T[::-1, :]

            xc, yc = self.DicoImager[iFacet]["pixCentral"]
            NpixFacet = self.DicoGridMachine[iFacet]["Dirty"][0].shape[2]


            Aedge, Bedge = GiveEdges((xc, yc), NPixOut,
                                     (NpixFacet/2, NpixFacet/2), NpixFacet)
            x0main, x1main, y0main, y1main = Aedge
            x0facet, x1facet, y0facet, y1facet = Bedge

            for Channel in xrange(self.VS.NFreqBands):
                ThisSumWeights = self.DicoImager[iFacet]["SumWeights"][Channel]
                ThisSumJones = self.DicoImager[iFacet]["SumJonesNorm"][Channel]
                T.timeit("3")
                for pol in xrange(npol):
                    # ThisSumWeights.reshape((nch,npol,1,1))[Channel, pol, 0, 0]
                    if kind == "Jones-amplitude":
                        Im = SpacialWeigth[::-1, :].T[x0facet:x1facet, y0facet:y1facet] * ThisSumJones
                    else:
                        if kind == "Dirty" or kind == "PSF":
                            Im = self.DicoGridMachine[iFacet]["Dirty"][Channel][pol].real.copy()
                        else:
                            raise RuntimeError,"unknown kind=%s argument -- this is a silly bug"%kind
                        # normalize by facet weight
                        sumweight = ThisSumWeights[pol]
                        Im /= SPhe.real
                        Im[SPhe < 1e-3] = 0
                        Im = (Im[::-1, :].T / sumweight)
                        Im /= np.sqrt(ThisSumJones)
                        Im *= SpacialWeigth[::-1, :].T
                        Im = Im[x0facet:x1facet, y0facet:y1facet]
                    Image[Channel, pol, x0main:x1main, y0main:y1main] += Im.real
            pBAR.render(int((iFacet+1)*100/float(NFacets)), '%4i/%i' % (iFacet+1, NFacets))

        for Channel in xrange(self.VS.NFreqBands):
            for pol in xrange(npol):
                Image[Channel, pol] /= self.NormImage

        return Image

    # def GiveNormImage(self):
    #     """
    #     Creates a stitched normalization image of the grid-correction function.
    #     This image should be point-wise divided from the stitched gridded map
    #     to create a grid-corrected map.
    #     Returns:
    #         stitched grid-correction norm image
    #     """
    #     Image = self.GiveEmptyMainField()
    #     nch, npol = self.nch, self.npol
    #     _, _, NPixOut, NPixOut = self.OutImShape
    #     SharedMemName = "%sSpheroidal" % (self.IdSharedMemData)
    #     NormImage = np.zeros((NPixOut, NPixOut), dtype=self.stitchedType)
    #     SPhe = NpShared.GiveArray(SharedMemName)
    #     N1 = self.NpixPaddedFacet
    #
    #     for iFacet in self.DicoImager.keys():
    #
    #         xc, yc = self.DicoImager[iFacet]["pixCentral"]
    #         Aedge, Bedge = GiveEdges((xc, yc), NPixOut, (N1/2, N1/2), N1)
    #         x0d, x1d, y0d, y1d = Aedge
    #         x0p, x1p, y0p, y1p = Bedge
    #
    #         for ch in xrange(nch):
    #             for pol in xrange(npol):
    #                 NormImage[x0d:x1d, y0d:y1d] += SPhe[::-1,
    #                                                     :].T.real[x0p:x1p, y0p:y1p]
    #
    #     return NormImage


    def ReinitDirty(self):
        """
        Reinitializes dirty map and weight buffers for the next round
        of residual calculation
        Postconditions:
        Resets the following:
            self.DicoGridMachine[iFacet]["Dirty"],
            self.DicoImager[iFacet]["SumWeights"],
            self.DicoImager[iFacet]["SumJones"]
            self.DicoImager[iFacet]["SumJonesChan"]
        Also sets up self._facet_grids as a dict of facet numbers to shared grid arrays.
        """
        self.SumWeights.fill(0)
        self.IsDirtyInit = True
        self.HasFourierTransformed = False
        # are we creating a new grids dict?
        if self._facet_grids is None:
            self._facet_grids = SharedDict.create("PSFGrid" if self.DoPSF else "Grid")

        for iFacet in self.DicoGridMachine.keys():
            NX = self.DicoImager[iFacet]["NpixFacetPadded"]
            # init or zero grid array
            grid = self._facet_grids.get(iFacet)
            if grid is None:
                grid = self._facet_grids.addSharedArray(iFacet, (self.VS.NFreqBands, self.npol, NX, NX), self.CType)
            else:
                grid.fill(0)
            self.DicoGridMachine[iFacet]["Dirty"] = grid
            self.DicoImager[iFacet]["SumWeights"] = np.zeros((self.VS.NFreqBands, self.npol), np.float64)
            self.DicoImager[iFacet]["SumJones"] = np.zeros((2, self.VS.NFreqBands), np.float64)
            self.DicoImager[iFacet]["SumJonesChan"] = []
            for iMS in xrange(self.VS.nMS):
                nVisChan = self.VS.ListMS[iMS].ChanFreq.size
                self.DicoImager[iFacet]["SumJonesChan"].append(np.zeros((2, nVisChan), np.float64))

    def applySparsification(self, DATA, factor):
        """Computes a sparsification vector for use in the BDA gridder. This is a vector of bools,
        same size as the number of BDA blocks, with a True for every block that will be gridded.
        Blocks ae chosen at random with a probability of 1/factor"""
        if not factor or "BDA.Grid" not in DATA:
            DATA["Sparsification"] = np.array([])
        else:
            # randomly select blocks with 1/sparsification probability
            num_blocks = DATA["BDA.Grid"][0]
            DATA["Sparsification.Grid"] = numpy.random.sample(num_blocks) < 1.0 / factor
            print>> log, "applying sparsification factor of %f to %d BDA grid blocks, left with %d" % (factor, num_blocks, DATA["Sparsification.Grid"].sum())
            #num_blocks = DATA["BDADegrid"][0]
            #DATA["Sparsification.Degrid"] = numpy.random.sample(num_blocks) < 1.0 / factor
            #print>> log, "applying sparsification factor of %f to %d BDA degrid blocks, left with %d" % (factor, num_blocks, DATA["Sparsification.Degrid"].sum())

    def _reload_worker_dicts (self, iFacet, datadict_path, cfdict_path, griddict_path):
        """Helper method for worker methods. Reloads various shared dicts, if needed"""
        # reload data dict, if this process has an old one
        if datadict_path:
            if self.DATA is None or datadict_path != self.DATA.path:
                del self.DATA
                self.DATA = SharedDict.attach(datadict_path)
        # reload CF dict, if this process has a different one
        if cfdict_path:
            if self._CF is None or self._CF.path != cfdict_path or iFacet not in self._CF:
                del self._CF
                self._CF = SharedDict.attach(cfdict_path)
            cf_dict = self._CF[iFacet]
        else:
            cf_dict = None
        # reload facet grids, if this process has an old one
        if griddict_path:
            if self._facet_grids is None or griddict_path != self._facet_grids.path:
                del self._facet_grids
                self._facet_grids = SharedDict.attach(griddict_path)
        # return facet's CF dict
        return cf_dict

    def _grid_worker(self, iFacet, datadict_path, cfdict_path, griddict_path):
        T = ClassTimeIt.ClassTimeIt()
        T.disable()
        ## FFTW wisdom already loaded by main process
        # if FFTW_Wisdom is not None:
        #     pyfftw.import_wisdom(FFTW_Wisdom)
        # T.timeit("%s: import wisdom" % iFacet)

        # reload shared dicts
        cf_dict = self._reload_worker_dicts(iFacet, datadict_path, cfdict_path, griddict_path)
        # Create a new GridMachine
        GridMachine = self._createGridMachine(iFacet, cf_dict=cf_dict,
            bda_grid=self.DATA["BDA.Grid"], bda_degrid=self.DATA["BDA.Degrid"])
        T.timeit("%s: create GM" % iFacet)

        uvwThis = self.DATA["uvw"]
        visThis = self.DATA["data"]
        flagsThis = self.DATA["flags"]
        times = self.DATA["times"]
        A0 = self.DATA["A0"]
        A1 = self.DATA["A1"]
        A0A1 = A0, A1
        W = self.DATA["Weights"]  ## proof of concept for now
        freqs = self.DATA["freqs"]
        ChanMapping = self.DATA["ChanMapping"]

        DecorrMode = self.GD["RIME"]["DecorrMode"]
        if ('F' in DecorrMode) | ("T" in DecorrMode):
            uvw_dt = self.DATA["uvw_dt"]
            DT, Dnu = self.DATA["MSInfos"]
            lm_min=None
            if self.GD["RIME"]["DecorrLocation"]=="Edge":
                lm_min=self.DicoImager[iFacet]["lm_min"]
            GridMachine.setDecorr(uvw_dt, DT, Dnu, 
                                  SmearMode=DecorrMode, 
                                  lm_min=lm_min,
                                  lm_PhaseCenter=self.DATA["lm_PhaseCenter"])

        # DecorrMode = GD["DDESolutions"]["DecorrMode"]
        # if ('F' in DecorrMode) or ("T" in DecorrMode):
        #     uvw_dt = DATA["uvw_dt"]
        #     DT, Dnu = DATA["MSInfos"]
        #     GridMachine.setDecorr(uvw_dt, DT, Dnu, SmearMode=DecorrMode)

        # Create Jones Matrices Dictionary
        DicoJonesMatrices = None
        Apply_killMS = self.GD["DDESolutions"]["DDSols"]
        Apply_Beam = self.GD["Beam"]["Model"] is not None

        if Apply_killMS or Apply_Beam:
            DicoJonesMatrices = {}
        if Apply_killMS:
            DicoJonesMatrices["DicoJones_killMS"] = self.DATA["killMS"]
        if Apply_Beam:
            DicoJonesMatrices["DicoJones_Beam"] = self.DATA["Beam"]


        GridMachine.put(times, uvwThis, visThis, flagsThis, A0A1, W,
                        DoNormWeights=False,
                        DicoJonesMatrices=DicoJonesMatrices,
                        freqs=freqs, DoPSF=self.DoPSF,
                        ChanMapping=ChanMapping,
                        ResidueGrid=self._facet_grids[iFacet],
                        sparsification=self.DATA.get("Sparsification.Grid")
                        )
        T.timeit("put %s" % iFacet)

        T.timeit("Grid")
        Sw = GridMachine.SumWeigths.copy()
        SumJones = GridMachine.SumJones.copy()
        SumJonesChan = GridMachine.SumJonesChan.copy()

        return {"iFacet": iFacet, "Weights": Sw, "SumJones": SumJones, "SumJonesChan": SumJonesChan}

    def gridChunkInBackground(self, DATA):
        """
        Grids a chunk of input visibilities onto many facets. Issues jobs to the compute threads.
        Visibility data is already in the data shared dict.

        """
        # wait for any init to finish
        self.awaitInitCompletion()
        # wait for any previous gridding/degridding jobs to finish, if still active
        self.collectGriddingResults()
        self.collectDegriddingResults()
        # run new set of jobs
        self._grid_iMS, self._grid_iChunk = DATA["iMS"], DATA["iChunk"]
        self._grid_job_label = DATA["label"]
        self._grid_job_id = "%s.Grid.%s:" % (self._app_id, self._grid_job_label)
        for iFacet in self.DicoImager.keys():
            APP.runJob("%sF%d" % (self._grid_job_id, iFacet), self._grid_worker,
                            args=(iFacet, DATA.path, self._CF.path, self._facet_grids.path))

    def collectGriddingResults(self):
        """
        If any grid workers are still at work, waits for them to finish and collects the results.
        Otherwise does nothing.

        Post conditions:
            Updates the following normalization weights, as produced by the gridding process:
                self.DicoImager[iFacet]["SumWeights"]
                self.DicoImager[iFacet]["SumJones"]
                self.DicoImager[iFacet]["SumJonesChan"][DATA["iMS"]]
        """
        # if this is set to None, then results already collected
        if self._grid_job_id is None:
            return
        # collect results of grid workers
        results = APP.awaitJobResults(self._grid_job_id+"*",progress=
                            ("Grid PSF %s" if self.DoPSF else "Grid %s") % self._grid_job_label)
        for DicoResult in results:
            # if we hit a returned exception, raise it again
            if isinstance(DicoResult, Exception):
                raise DicoResult
            iFacet = DicoResult["iFacet"]
            self.DicoImager[iFacet]["SumWeights"] += DicoResult["Weights"]
            self.DicoImager[iFacet]["SumJones"] += DicoResult["SumJones"]
            self.DicoImager[iFacet]["SumJonesChan"][self._grid_iMS] += DicoResult["SumJonesChan"]
        self._grid_job_id = None
        return True

    def _fft_worker(self, iFacet, cfdict_path, griddict_path):
        """
        Fourier transforms the grids currently housed in shared memory
        Precondition:
            Should be called after all data has been gridded
        Returns:
            Dictionary of success and facet identifier
        """
        # reload shared dicts
        cf_dict = self._reload_worker_dicts(iFacet, None, cfdict_path, griddict_path)
        GridMachine = self._createGridMachine(iFacet, cf_dict=cf_dict)
        Grid = self._facet_grids[iFacet]
        # note that this FFTs in-place
        GridMachine.GridToIm(Grid)
        return {"iFacet": iFacet}

    def fourierTransformInBackground(self):
        '''
        Fourier transforms the individual facet grids in-place.
        Runs background jobs for this.
        '''
        # wait for any previous gridding jobs to finish, if still active
        self.collectGriddingResults()
        # run FFT jobs
        self._fft_job_id = "%s.FFT:" % self._app_id
        for iFacet in self.DicoImager.keys():
            APP.runJob("%sF%d" % (self._fft_job_id, iFacet), self._fft_worker,
                            args=(iFacet, self._CF.path, self._facet_grids.path),
                            )

    def collectFourierTransformResults (self):
        if self._fft_job_id is None:
            return
        # collect results of FFT workers
        # (use label of previous gridding job for the progress bar)
        APP.awaitJobResults(self._fft_job_id+"*", progress=("FFT PSF" if self.DoPSF else "FFT"))
        self._fft_job_id = None

    # DeGrid worker that is called by Multiprocessing.Process
    def _degrid_worker(self, iFacet, datadict_path, cfdict_path, griddict_path, modeldict_path, model_id, ChanSel):
        """
        Args:
            iFacet:         facet number
            datadict_path:  path to DATA shared dict
            cfdict_path:    path to CF shared dict
            griddict_path:  path to Grids shared dict
            modeldict_path: path to Model shared dict
            model_id:       id of model object (used to determine when to reload the Model dict)
            ChanSel:        channel selection
            predict:        if True, DATA["predict"] will be populated with degridded visibilities
            subtract:       if True, DATA["data"] will have degridded visibilities subtracted in place
        """
        # reload shared dicts
        cf_dict = self._reload_worker_dicts(iFacet, datadict_path, cfdict_path, griddict_path)
        # reload model image dict, if serial number has changed
        if self._model_dict is None or self._model_dict.get("Id") != model_id:
            self._model_dict = SharedDict.attach(modeldict_path)

        print>>log, ModColor.Str("PROBLEM for OLEG")
        self._model_dict = SharedDict.attach(modeldict_path)

        # We get the psf dict directly from the shared dict name (not from the .path of a SharedDict)
        # because this facet machine is not necessarilly the one where we have computed the PSF
        self._psf_dict  = SharedDict.attach("dictPSF")
        # extract facet model from model image
        ModelGrid, _ = self._Im2Grid.GiveModelTessel(self._model_dict["Image"],
                                                     self.DicoImager, iFacet, self._psf_dict["NormImage"],
                                                     cf_dict["Sphe"], cf_dict["SW"], ChanSel=ChanSel)

        # Create a new GridMachine
        GridMachine = self._createGridMachine(iFacet, cf_dict=cf_dict,
            ListSemaphores=ClassFacetMachine._degridding_semaphores,
            bda_grid=self.DATA["BDA.Grid"], bda_degrid=self.DATA["BDA.Degrid"])

        uvwThis = self.DATA["uvw"]
        visThis = self.DATA["data"]
        flagsThis = self.DATA["flags"]
        times = self.DATA["times"]
        A0 = self.DATA["A0"]
        A1 = self.DATA["A1"]

        A0A1 = A0, A1
        freqs = self.DATA["freqs"]
        ChanMapping = self.DATA["ChanMappingDegrid"]

        # Create Jones Matrices Dictionary
        DicoJonesMatrices = None
        Apply_killMS = self.GD["DDESolutions"]["DDSols"]
        Apply_Beam = self.GD["Beam"]["Model"] is not None

        if Apply_killMS or Apply_Beam:
            DicoJonesMatrices = {}
        if Apply_killMS:
            DicoJonesMatrices["DicoJones_killMS"] = self.DATA["killMS"]
        if Apply_Beam:
            DicoJonesMatrices["DicoJones_Beam"] = self.DATA["Beam"]

        DecorrMode = self.GD["RIME"]["DecorrMode"]
        if ('F' in DecorrMode) | ("T" in DecorrMode):
            uvw_dt = self.DATA["uvw_dt"]
            DT, Dnu = self.DATA["MSInfos"]
            lm_min=None
            if self.GD["RIME"]["DecorrLocation"]=="Edge":
                lm_min=self.DicoImager[iFacet]["lm_min"]
            GridMachine.setDecorr(uvw_dt, DT, Dnu, 
                                  SmearMode=DecorrMode, 
                                  lm_min=lm_min,
                                  lm_PhaseCenter=self.DATA["lm_PhaseCenter"])

        GridMachine.get(times, uvwThis, visThis, flagsThis, A0A1,
                          ModelGrid, ImToGrid=False,
                          DicoJonesMatrices=DicoJonesMatrices,
                          freqs=freqs, TranformModelInput="FT",
                          ChanMapping=ChanMapping,
                          sparsification=self.DATA.get("Sparsification.Degrid")
                        )

        return {"iFacet": iFacet}

    def degridChunkInBackground (self, DATA):
        """
        Degrids visibilities from model image. The model image is unprojected
        into many facets before degridding and subtracting each of the model
        facets contributions from the residual image.
        Preconditions: the dirty image buffers should be cleared before calling
        the predict and regridding methods
        to construct a new residual map
        Args:
            times:
            uvwIn:
            visIn:
            flag:
            A0A1:
            ModelImage:
        """
        # wait for any init to finish
        self.awaitInitCompletion()

        # run new set of jobs
        ChanSel = sorted(set(DATA["ChanMappingDegrid"]))  # unique channel numbers for degrid

        self._degrid_job_label = DATA["label"]
        self._degrid_job_id = "%s.Degrid.%s:" % (self._app_id, self._degrid_job_label)

        for iFacet in self.DicoImager.keys():
            APP.runJob("%sF%d" % (self._degrid_job_id, iFacet), self._degrid_worker,
                            args=(iFacet, DATA.path, self._CF.path, self._facet_grids.path,
                                  self._model_dict.path, self._model_dict["Id"], ChanSel))
            # APP.runJob("%sF%d" % (self._degrid_job_id, iFacet), self._degrid_worker,
            #                 args=(iFacet, DATA.path, self._CF.path, self._facet_grids.path,
            #                       self._model_dict.path, self._model_dict["Id"], ChanSel),
            #            serial=True)


    def collectDegriddingResults(self):
        """
        If any degrid workers are still at work, waits for them to finish and collects the results.
        Otherwise does nothing.
        """
        # if this is set to None, then results already collected
        if self._degrid_job_id is None:
            return
        # collect results of degrid workers
        APP.awaitJobResults(self._degrid_job_id + "*", progress="Degrid %s" % self._degrid_job_label)
        self._degrid_job_id = None
        return True

