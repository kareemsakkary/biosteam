# -*- coding: utf-8 -*-
# BioSTEAM: The Biorefinery Simulation and Techno-Economic Analysis Modules
# Copyright (C) 2020-2023, Yoel Cortes-Pena <yoelcortes@gmail.com>
# 
# This module is under the UIUC open-source license. See 
# github.com/BioSTEAMDevelopmentGroup/biosteam/blob/master/LICENSE.txt
# for license details.
"""
This module contains unit operations for the separation of solids 
(e.g. centrifugation, expression, filtration).

.. contents:: :local:
    
.. autoclass:: biosteam.units.solids_separation.SolidsSeparator
.. autoclass:: biosteam.units.solids_separation.SolidsCentrifuge
.. autoclass:: biosteam.units.solids_separation.RotaryVacuumFilter 
.. autoclass:: biosteam.units.solids_separation.PressureFilter
.. autoclass:: biosteam.units.solids_separation.ScrewPress

References
----------
.. [1] Seider, Warren D., et al. (2017). "Cost Accounting and Capital Cost 
    Estimation". In Product and Process Design Principles: Synthesis, Analysis,
    and Evaluation (pp. 481-485). New York: Wiley.
.. [2] Humbird, D., Davis, R., Tao, L., Kinchin, C., Hsu, D., Aden, A.,
    Dudgeon, D. (2011). Process Design and Economics for Biochemical 
    Conversion of Lignocellulosic Biomass to Ethanol: Dilute-Acid 
    Pretreatment and Enzymatic Hydrolysis of Corn Stover
    (No. NREL/TP-5100-47764, 1013269). https://doi.org/10.2172/1013269

"""
from .decorators import cost
from .splitting import Splitter
from .design_tools import compute_vacuum_system_power_and_cost
from warnings import warn
from ..exceptions import lb_warning, InfeasibleRegion
from .decorators import cost
from biosteam.utils import remove_undefined_chemicals, default_chemical_dict
import numpy as np
import biosteam as bst
from thermosteam import separations
from math import exp, log, ceil

__all__ = ('SolidsSeparator', 'RotaryVacuumFilter', 'CrushingMill', 
           'PressureFilter', 'SolidsCentrifuge', 'RVF',
           'ScrewPress',)

class SolidsSeparator(Splitter):
    """
    Create SolidsSeparator object.
    
    Parameters
    ----------
    ins : 
        Inlet fluids with solids.
    outs : 
        * [0] Retentate.
        * [1] Permeate.
    split : array_like
        Component splits to 0th output stream
    moisture_content : float
        Fraction of water in solids
    
    """
    _N_ins = 1
    _ins_size_is_fixed = False
    
    def _init(self, split, 
            order=None, moisture_content=None, 
            moisture_ID=None,
            strict_moisture_content=None
        ):
        Splitter._init(self, order=order, split=split)
        #: Moisture content of retentate
        self.moisture_content = moisture_content
        self.strict_moisture_content = strict_moisture_content
        if moisture_content is not None:
            if moisture_ID is None: moisture_ID = '7732-18-5'
            self.moisture_ID = moisture_ID
    
    def _run(self):
        if self.moisture_content is None:
            separations.mix_and_split(
                self.ins, *self.outs, self.split,
            )
        else:
            moisture_ID = self.moisture_ID
            self.isplit[moisture_ID] = 0.
            separations.mix_and_split_with_moisture_content(
                self.ins, *self.outs, self.split, self.moisture_content, self.moisture_ID,
                self.strict_moisture_content,
            )
    #     if self._recycle_system and self._system.algorithm == 'Phenomena oriented':
    #         ID = self.moisture_ID
    #         if not ID: return
    #         top, bottom = self.outs
    #         top_mol = top.imol[ID]
    #         self.isplit[ID] = top_mol / (top_mol + bottom.imol[ID])
            
    # def _update_nonlinearities(self):
    #     outs = self.outs
    #     data = [i.get_data() for i in outs]
    #     self._run()
    #     for i, j in zip(outs, data): i.set_data(j)


class SolidsCentrifuge(SolidsSeparator):
    """
    Create a solids centrifuge that separates out solids according to
    user defined split. Capital cost is based on [1]_.
    
    Parameters
    ----------
    ins : 
        Inlet fluid with solids.
    outs : 
        * [0] Solids-rich stream.
        * [1] Liquid-rich stream.
    split : array_like or dict[str, float]
           Component splits.
    order=None : Iterable[str]
        Species order of split. Defaults to Stream.chemicals.IDs.
    solids : tuple[str]
        IDs of solids.
    moisture_content : float
        Fraction of water in stream.
    centrifuge_type : str
        Type of the centrifuge, either 'reciprocating_pusher' (1-20 ton/hr solids)
        or 'scroll_solid_bowl' (2-40 ton/hr solids).
    
    """
    _units = {'Solids loading': 'ton/hr',
              'Flow rate': 'm3/hr'}
    solids_loading_range = {
    'reciprocating_pusher': (1, 20),
    'scroll_solid_bowl': (2, 40)
    }
    kWhr_per_m3 = 1.40


    def _init(self, split, order=None, solids=None, moisture_content=0.40,
              centrifuge_type='scroll_solid_bowl', moisture_ID=None,
              strict_moisture_content=None):
        SolidsSeparator._init(
            self, moisture_content=moisture_content,
            split=split, order=order, moisture_ID=moisture_ID,
            strict_moisture_content=strict_moisture_content
        )
        if solids is None:
            solids = [i.ID for i in self.chemicals if i.locked_state == 's']
        self.solids = solids
        self.centrifuge_type = centrifuge_type
    
    @property
    def solids(self):
        return self._solids
    @solids.setter
    def solids(self, solids):
        self._solids = tuple(solids)
    
    @property
    def centrifuge_type(self):
        return self._centrifuge_type
    @centrifuge_type.setter
    def centrifuge_type(self, i):
        if not i in ('reciprocating_pusher', 'scroll_solid_bowl'):
            raise ValueError('`centrifuge_type` can only be "reciprocating_pusher" or '
                            f'"scroll_solid_bowl", not {i}.')
        self._centrifuge_type = i

    def _design(self):
        solids, centrifuge_type = self._solids, self.centrifuge_type
        ts = sum([s.imass[solids].sum() for s in self.ins if not s.isempty()]) # Total solids
        ts *= 0.0011023 # To short tons (2000 lbs/hr)
        self.design_results['Solids loading'] = ts
        lb, ub = self.solids_loading_range[centrifuge_type]
        if ts < lb:
            lb_warning(self, 'Solids loading', ts, 'ton/hr', lb)
        self.design_results['Number of centrifuges'] = ceil(ts/ub)
        cost = 68040*(ts**0.5) if centrifuge_type else 170100*(ts**0.3)
        cost *= bst.CE / 567
        self.baseline_purchase_costs['Centrifuges'] = cost
        self.F_BM['Centrifuges'] = 2.03
        self.design_results['Flow rate'] = F_vol_in = self.F_vol_in
        self.power_utility(F_vol_in * self.kWhr_per_m3)


class RotaryVacuumFilter(SolidsSeparator):
    """
    Create a RotaryVacuumFilter object.
    
    Parameters
    ----------
    ins : 
        * [0] Feed
        * [1] Wash water
    outs :  
        * [0] Retentate
        * [1] Permeate
    split : array_like or dict[str, float]
           Component splits.
    moisture_content : float
                       Fraction of water in retentate.
    
    """
    auxiliary_unit_names = ('vacuum_system',)
    _F_BM_default = {'Vessels': 2.32,
                     'Vacuum system': 1.0}
    
    #: Revolutions per second
    rps = 20/3600
    
    #: Radius of the vessel (m)
    radius = 1
    
    #: Suction pressure (Pa)
    P_suction = 1500.
    
    #: For crystals (lb/day-ft^2)
    filter_rate = 6000
    
    _kwargs = {'moisture_content': 0.80} # fraction
    _bounds = {'Individual area': (10, 800)}
    _units = {'Area': 'ft^2',
              'Individual area': 'ft^2'}
    
    def _design(self):
        flow = sum([stream.F_mass for stream in self.outs])
        self.design_results['Area'] = self._calc_Area(flow, self.filter_rate)
        
    def _cost(self):
        Design = self.design_results
        Area = Design['Area']
        ub = self._bounds['Individual area'][1]
        N_vessels = np.ceil(Area/ub)
        iArea = Area/N_vessels # individual vessel
        self.parallel['self'] = N_vessels
        self.parallel['vacuum_system'] = 1
        Design['Individual area'] = iArea
        self._load_vacuum_system(Area, N_vessels)
        logArea = np.log(iArea)
        Cost = np.exp(11.796 - 0.1905 * logArea + 0.0554 * logArea**2)
        self.baseline_purchase_costs['Vessels'] = Cost * bst.CE/567
    
    def _load_vacuum_system(self, area, N_vessels):
        s_cake, s_vacuumed = self.outs
        radius = self.radius
        Area = self.design_results['Individual area']
        N = self.parallel['self']
        vacummed_air = s_vacuumed.F_vol # Flow rate sucked-in displaces air
        air_density = 1.2754 # kg /m3
        self.vacuum_system = bst.VacuumSystem(
            F_mass=vacummed_air * air_density, 
            F_vol=vacummed_air, 
            P_suction=self.P_suction, 
            vessel_volume=N * radius * Area * 0.0929 / 2., # m3
        )
        
    @staticmethod
    def _calc_Area(flow, filter_rate):
        """Return area in ft^2 given flow in kg/hr and filter rate in lb/day-ft^2."""
        return flow * 52.91 / filter_rate
    
RVF = RotaryVacuumFilter

@cost('Flow rate', units='kg/hr', cost=1.5e6, CE=541.7,
      n=0.6, S=335e3, kW=2010, BM=2.3)
class CrushingMill(SolidsSeparator):
    """
    Create crushing mill unit operation for the 
    separation of sugarcane juice from the bagasse.
    
    Parameters
    ----------
    ins : 
        * [0] Shredded sugar cane
        * [1] Recycle water
    outs :  
        * [0] Bagasse
        * [1] Juice
    split : array_like or dict[str, float]
        Splits of chemicals to the bagasse.
    moisture_content : float
                       Fraction of water in Bagasse.
    
    """

_hp2kW = 0.7457
@cost('Retentate flow rate', 'Flitrate tank agitator',
      cost=26e3, CE=551, kW=7.5*_hp2kW, S=31815, n=0.5, BM=1.5)
@cost('Retentate flow rate', 'Discharge pump',
      cost=13040, CE=551, S=31815, n=0.8, BM=2.3)
@cost('Retentate flow rate', 'Filtrate tank',
      cost=103e3, S=31815, CE=551, BM=2.0, n=0.7)
@cost('Retentate flow rate', 'Feed pump', kW=74.57,
      cost= 18173, S=31815, CE=551, n=0.8, BM=2.3)
@cost('Retentate flow rate', 'Stillage tank 531',
      cost=174800, CE=551, S=31815, n=0.7, BM=2.0)
@cost('Retentate flow rate', 'Mafifold flush pump', kW=74.57,
      cost=17057, CE=551, S=31815, n=0.8, BM=2.3)
@cost('Retentate flow rate', 'Recycled water tank',
      cost=1520, CE=551, S=31815, n=0.7, BM=3.0)
@cost('Retentate flow rate', 'Wet cake screw',  kW=15*_hp2kW,
      cost=2e4, CE=521.9, S=28630, n=0.8, BM=1.7)
@cost('Retentate flow rate', 'Wet cake conveyor', kW=10*_hp2kW,
      cost=7e4, CE=521.9, S=28630, n=0.8, BM=1.7)
@cost('Retentate flow rate', 'Pressure filter',
      cost=3294700, CE=551, S=31815, n=0.8, BM=1.7)
@cost('Retentate flow rate', 'Pressing air compressor receiver tank',
      cost=8e3, CE=551, S=31815, n=0.7, BM=3.1)
@cost('Retentate flow rate', 'Cloth wash pump', kW=150*_hp2kW,
      cost=29154, CE=551, S=31815, n=0.8, BM=2.3)
@cost('Retentate flow rate', 'Dry air compressor receiver tank',
      cost=17e3, CE=551, S=31815, n=0.7, BM=3.1)
@cost('Retentate flow rate', 'Pressing air pressure filter',
      cost=75200, CE=521.9, S=31815, n=0.6, kW=112, BM=1.6)
@cost('Retentate flow rate', 'Dry air pressure filter (2)',
      cost=405000, CE=521.9, S=31815, n=0.6, kW=1044, BM=1.6)
class PressureFilter(SolidsSeparator):
    """
    Create a pressure filter for the separation of structural carbohydrates, 
    lignin, cell mass, and other solids. Capital costs are based on [2]_.
    
    Parameters
    ----------
    ins : 
        Contains structural carbohydrates, lignin, cell mass, and other solids.
    outs : 
        * [0] Retentate (i.e. solids)
        * [1] Filtrate
    split : array_like or dict[str, float]
        Splits of chemicals to the retentate. Defaults to values used in
        the 2011 NREL report on cellulosic ethanol as given in [2]_.
    moisture_content : float, optional
        Moisture content of retentate. Defaults to 0.35
    
    """
    _units = {'Retentate flow rate': 'kg/hr'}
    
    def _init(self, moisture_content=0.35, split=None):
        if split is None:
            chemicals = self.chemicals
            split = dict(
                Furfural=0.03571,
                Glycerol=0.03714,
                LacticAcid=0.03727,
                SuccinicAcid=0.03714,
                HNO3=0.03716,
                Denaturant=0.03714,
                DAP=0.03716,
                AmmoniumAcetate=0.03727,
                AmmoniumSulfate=0.03716,
                NaNO3=0.03716,
                Oil=0.03714,
                HMF=0.03571,
                Glucose=0.03647,
                Xylose=0.03766,
                Sucrose=0.0359,
                Mannose=0.0359,
                Galactose=0.0359,
                Arabinose=0.0359,
                Extract=0.03727,
                Tar=0.9799,
                CaO=0.9799,
                Ash=0.9799,
                NaOH=0.03716,
                Lignin=0.98,
                SolubleLignin=0.03727,
                GlucoseOligomer=0.03722,
                GalactoseOligomer=0.03722,
                MannoseOligomer=0.03722,
                XyloseOligomer=0.03722,
                ArabinoseOligomer=0.03722,
                Z_mobilis=0.9799,
                T_reesei=0.9799,
                Protein=0.98,
                Enzyme=0.98,
                Glucan=0.9801,
                Xylan=0.9811,
                Xylitol=0.03714,
                Cellobiose=0.0359,
                DenaturedEnzyme=0.98,
                Arabinan=0.9792,
                Mannan=0.9792,
                Galactan=0.9792,
                WWTsludge=0.9799,
                Cellulase=0.03727
            )
            remove_undefined_chemicals(split, chemicals)
            default_chemical_dict(split, chemicals, 0.03714, 0.03714, 0.9811)
        bst.SolidsSeparator._init(self, moisture_content=moisture_content, split=split)
    
    def _design(self):
        self.design_results['Retentate flow rate'] = self.outs[0].F_mass

PressureFilter._stacklevel += 1

#: TODO: Check BM assumption. Use 1.39 for crushing unit operations for now.
@cost('Flow rate', units='lb/hr', CE=567, lb=150, ub=12000, BM=1.39, 
      f=lambda S: exp((11.0991 - 0.3580*log(S) + 0.05853*log(S)**2)))
class ScrewPress(SolidsSeparator):
    """
    Create screw press unit operation for the 
    expression of liquids from solids. Capital cost is based on [1]_.
    
    Parameters
    ----------
    ins : 
        * [0] Solids + liquid
    outs :  
        * [1] Solids (retentate)
        * [0] Liquids (permeate)
    split : array_like or dict[str, float]
           Component splits.
    moisture_content : float
        Fraction of water in solids.
                  
    
    """ 
    kWh_per_bmt = 37.2 # From Perry's Handbook, 18-126
    # Energy consumption may be drastically different depending on the application
    # - 5 to 12 bdmt (tonne dry biomass) https://www.andritz.com/products-en/group/pulp-and-paper/service-solutions/screw-press-service/screw-press-upgrade-case-study-1-less
    
    def _cost(self):
        self._decorated_cost()
        biomass = self.ins[0]
        bmt = biomass.F_mass * 0.001
        self.add_power_utility(bmt * self.kWh_per_bmt)