"""
A module to build/load a Data Portal for myopic run using both SQL to pull data
and python to filter results
"""
import time
from collections import defaultdict
from logging import getLogger
from sqlite3 import Connection
from typing import Sequence

from pyomo.core import Param, Set
from pyomo.dataportal import DataPortal

from temoa.temoa_model.myopic.myopic_index import MyopicIndex
from temoa.temoa_model.temoa_model import TemoaModel

"""
Tools for Energy Model Optimization and Analysis (Temoa):
An open source framework for energy systems optimization modeling

Copyright (C) 2015,  NC State University

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

A complete copy of the GNU General Public License v2 (GPLv2) is available
in LICENSE.txt.  Users uncompressing this from an archive may not have
received this license file.  If not, see <http://www.gnu.org/licenses/>.


Written by:  J. F. Hyink
jeff@westernspark.us
https://westernspark.us
Created on:  1/21/24

"""

logger = getLogger(__name__)

# the tables below are ones in which we might find regional groups which should be captured
# to make the members of the RegionalGlobalIndices Set in the model.  They need to aggregated
tables_with_regional_groups = {'MaxActivity': 'regions',
                               'MinActivity': 'regions',
                               'MinAnnualCapacityFactor': 'regions',
                               'MaxAnnualCapacityFactor': 'regions',
                               'EmissionLimit': 'regions',
                               'MinActivityGroup': 'regions',
                               'MaxActivityGroup': 'regions',
                               'MinCapacityGroup': 'regions',
                               'MaxCapacityGroup': 'regions',
                               }

class HybridLoader:
    """
    An instance of the HybridLoader
    """

    def __init__(self, db_connection: Connection):
        self.debugging = False  # for T/S
        self.con = db_connection

        # filters for myopic ops
        self.viable_techs: set[str] = set()
        self.viable_comms: set[str] = set()
        self.viable_input_comms: set[str] = set()
        self.viable_output_comms: set[str] = set()
        self.viable_vintages: set[int] = set()
        self.viable_rtv: set[tuple[str, str, int]] = set()
        self.efficiency_values: list[tuple] = []

    def _refresh_filters(self, myopic_index: MyopicIndex):
        """
        refresh all the sets used for filtering from the current contents
        of the MyopicEfficiency table.  This should normally be called
        after a Myopic iteration where MyopicEfficiency is updated
        :return:
        """
        cur = self.con.cursor()
        self._clear_filters()
        # we need to union a couple things...
        # 1.  Everything from the MyopicEfficiency table that is still "alive" (within lifespan)
        # 2.  Everything in the original Efficiency table that is now "in view" for this
        #     myopic index
        # We can just use '1' for the lifetime of the existing stuff, because the "test" is just
        # to be greater than the current vintage.
        contents = cur.execute(
            'SELECT region, input_comm, tech, vintage, output_comm, efficiency, lifetime  '
            'FROM MyopicEfficiency '
            f'WHERE vintage + lifetime > {myopic_index.base_year}'
        ).fetchall()
        logger.debug('polled %d elements from MyopicEfficiency table', len(contents))

        # We will need to ID the physical commodities...
        raw = cur.execute(
            "SELECT comm_name FROM main.commodities WHERE flag = 'p'"
        ).fetchall()
        phys_commodities = {t[0] for t in raw}
        assert len(phys_commodities) > 0, 'Failsafe...we should find some!  Flag change?'
        # We will need to iterate a bit here to:
        # 1.  Screen all the input commodities -> viable commodities
        # 2.  Find any tech with PHYSICAL output commodity that is not viable (not in set above)
        # 3.  Suppress that tech
        # 4.  re-screen the input commodities
        # 5.  quit when no more techs are suppressed
        # 6.  Generate viable commodities & viable techs

        # we probably need to keep track of (r, t, v) tuples here because you *could*
        # have a tech process different things in different vintages/regions based solely
        # on Efficiency Table

        techs_by_output = defaultdict(set)
        ok_techs = set()
        suppressed_techs = set()
        viable_phys_commodities = set()
        for r, c1, t, v, c2, eff, lifetime in contents:
            row = (r, c1, t, v, c2, eff)
            if c1 not in phys_commodities:
                raise ValueError(f'Tech {t} has a non-physical input: {c1}')
            viable_phys_commodities.add(c1)
            ok_techs.add(row)
            # we screen here to not worry about demand/emission outputs
            if c2 in phys_commodities:
                techs_by_output[c2].add(row)

        illegal_outputs = set(techs_by_output.keys()) - viable_phys_commodities
        while illegal_outputs:
            for output in illegal_outputs:
                # mark all the techs that push that output as "suppressed"
                suppressed_techs.update(techs_by_output[output])
            # remove from viable
            ok_techs -= suppressed_techs
            # re-capture tech by output
            techs_by_output.clear()
            for row in ok_techs:
                r, c1, t, v, c2, eff = row
                if c2 in phys_commodities:
                    techs_by_output[c2].add(row)

            # re-screen for a new list of viable commodities from inputs
            viable_phys_commodities = {row[1] for row in ok_techs}
            illegal_outputs = set(techs_by_output.keys()) - viable_phys_commodities

        # log the deltas
        logger.debug('Reduced techs in Efficiency from %d to %d', len(contents), len(ok_techs))
        logger.debug('Reduced Physical Commodities from %d to %d', len(raw), len(viable_phys_commodities))
        # for tech in sorted(suppressed_techs, key = lambda tech: tech[2]):
        #     print(tech)
        for tech in suppressed_techs:
            logger.info('Tech: %s\n'
                        ' is SUPPRESSED as it has a Physical Commodity output that has no viable '
                        'receiver', tech)

        for row in ok_techs:
            r, c1, t, v, c2, _ = row
            self.viable_techs.add(t)
            self.viable_input_comms.add(c1)
            self.viable_rtv.add((r, t, v))
            self.viable_vintages.add(v)
            self.viable_output_comms.add(c2)
        self.viable_comms = self.viable_input_comms | self.viable_output_comms

        # book the EfficiencyTable
        # we should sort here for deterministic results after pulling from set
        self.efficiency_values = sorted(ok_techs)


    def _clear_filters(self):
        self.viable_techs.clear()
        self.viable_input_comms.clear()
        self.viable_output_comms.clear()
        self.viable_comms.clear()
        self.viable_rtv.clear()
        self.viable_vintages.clear()
        self.efficiency_values.clear()

    def table_exists(self, table_name: str) -> bool:
        """
        Check if a table exists in the schema... for use with "optional" tables
        :param table_name: the table name to check
        :return: True if it exists in the schema
        """
        table_name_check = self.con.cursor().execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'").fetchall()
        if len(table_name_check) > 0:
            return True
        return False

    @staticmethod
    def _efficiency_table_cleanup(raw_data: list[tuple], physical_commodities: Sequence) \
            -> list[tuple]:
        """
        A cleanup for the data going in to the efficiency table.  Needed to ensure that there
        are no cases where a commodity can be an input before it is available as an output
        or vice-versa.  Filtering only occurs after the base year passed in, so anything
        "existing" is ignored
        :param raw_data: the query result
        :param physical_commodities: the first year to apply the filtering on
        :return: filtered data set for Efficiency, reduced list of physical commodities
        """

        if len(physical_commodities) == 0:
            raise ValueError('No production commodities provided to efficiency filter')
        if not isinstance(physical_commodities, set):
            physical_commodities = set(physical_commodities)

        visible_inputs = {input_comm for
                          region, input_comm, tech, vintage, output_comm, efficiency in raw_data}
        visible_outputs = {output_comm for
                           region, input_comm, tech, vintage, output_comm, efficiency in raw_data}

        filtered_data = []
        for row in raw_data:
            region, input_comm, tech, vintage, output_comm, efficiency = row
            if output_comm in physical_commodities and output_comm not in visible_inputs:
                logger.warning('For Efficiency table entry: \n'
                               '    %s\n'
                               '    There are no sources to accept the physical commodity output: '
                               '%s\n'
                               '    Either the commodity is mislabeled as physical or this tech is '
                               'ahead of need.  This Efficiency entry is SUPPRESSED.',
                               row, output_comm)
            else:
                filtered_data.append(row)
            if input_comm not in visible_outputs:
                logger.warning('For Efficiency table entry: \n'
                               '    %s\n'
                               '    There are no sources to supply the input commodity: '
                               '%s\n'
                               '    Advisory only, no action taken.',row, input_comm)

        return filtered_data


    def load_data_portal(self, myopic_index: MyopicIndex | None = None) -> DataPortal:
        # the general plan:
        # 1. iterate through the model elements that are directly read from data
        # 2. use SQL query to get the full table
        # 3. (OPTIONALLY) filter it, as needed for myopic
        # 4. load it into the data dictionary
        tic = time.time()

        if myopic_index is not None and not isinstance(myopic_index, MyopicIndex):
            raise ValueError(f'received an illegal entry for the myopic index: {myopic_index}')
        else:
            mi = myopic_index  # abbreviated name
            self._refresh_filters(myopic_index=mi)

        # housekeeping
        data: dict[str, list | dict] = dict()

        def load_element(
            c: Set | Param, values: Sequence[tuple], validation: set | None = None, val_loc:
                tuple = None
        ):
            """
            Helper to alleviate some typing!
            Expects that the values passed in are an iterable of tuples, like a standard
            query result.
            :param c: the model component
            :param values: the keys for param or the item values for set as tuples
            :param validation: the set to validate the keys/set value against
            :param val_loc: tuple of the positions of r, t, v in the key for validation
            :return: None
            """
            if len(values) == 0:
                logger.info('no values for param or set: %s', c.name)
                return
            if not isinstance(values[0], tuple):
                raise ValueError('values must be an iterable of tuples')

            match c:
                case Set():  # it is a pyomo Set
                    # check for multi-dim sets (none expected)
                    if len(values) > 0 and len(values[0]) > 1:
                        raise ValueError(
                            'Encountered a multi-dimensional Set during data load. '
                            '\nNot currently supported'
                        )

                    if validation and mi:
                        data[c.name] = [t[0] for t in values if t[0] in validation]
                    else:
                        data[c.name] = [t[0] for t in values]
                case Param():  # c is a pyomo Param
                    if validation and mi:
                        if validation is self.viable_rtv:
                            if not val_loc:
                                raise ValueError(
                                    'Trying to validate against r, t, v and got no locations'
                                )
                            data[c.name] = {
                                t[:-1]: t[-1]
                                for t in values
                                if (t[val_loc[0]], t[val_loc[1]], t[val_loc[2]]) in self.viable_rtv
                            }
                            # quick check for region-groups which shouldn't show up here...
                            regions_screened = {t[val_loc[0]] for t in values}
                            groups_discovered = {t for t in regions_screened if '+' in t}
                            if len(groups_discovered) > 0:
                                logger.error('Region-Groups discovered during screen of:  %s.'
                                             'likely error in loader / param description.',
                                             c.name)
                        else:
                            if val_loc:
                                data[c.name] = {
                                    t[:-1]: t[-1] for t in values if t[val_loc[0]] in validation
                                }
                            else:
                                data[c.name] = {
                                    t[:-1]: t[-1] for t in values if t[:-1] in validation
                                }
                    else:
                        data[c.name] = {t[:-1]: t[-1] for t in values}
                case _:
                    raise ValueError(f'Component type unrecognized: {c}, {type(c)}')

        M: TemoaModel = TemoaModel()  # for typing purposes only
        cur = self.con.cursor()

        #   === TIME SETS ===

        # time_exist
        if mi:
            raw = cur.execute(
                f'SELECT t_periods from main.time_periods WHERE t_periods < {mi.base_year}'
            ).fetchall()
        else:
            raw = cur.execute(
                f"SELECT t_periods from main.time_periods WHERE flag = 'e'"
            ).fetchall()
        load_element(M.time_exist, raw)

        # time_future
        if mi:
            raw = cur.execute(
                'SELECT t_periods from main.time_periods WHERE '
                f't_periods >= {mi.base_year} AND t_periods <= {mi.last_year}',
            ).fetchall()
        else:
            raw = cur.execute(
                f"SELECT t_periods from main.time_periods WHERE flag = 'f'"
            ).fetchall()
        load_element(M.time_future, raw)

        # time_of_day
        raw = cur.execute('SELECT t_day from main.time_of_day').fetchall()
        load_element(M.time_of_day, raw)

        # time_season
        raw = cur.execute('SELECT t_season from main.time_season').fetchall()
        load_element(M.time_season, raw)

        #  === REGION SETS ===

        # regions
        raw = cur.execute('SELECT regions from main.regions').fetchall()
        load_element(M.regions, raw)

        # region-groups  (these are the R1+R2, R1+R4+R6 type region labels)
        regions_and_groups = set()
        for table, field_name in tables_with_regional_groups.items():
            if self.table_exists(table):
                raw = cur.execute(f'SELECT {field_name} from main.{table}').fetchall()
                regions_and_groups.update({t[0] for t in raw})
        # filter to those that contain "+" and sort (for deterministic pyomo behavior)
        list_of_groups = sorted((t, ) for t in regions_and_groups) #if "+" in t or t=='global')
        load_element(M.RegionalGlobalIndices, list_of_groups)

        # region-exchanges
        # TODO:  Perhaps tease the exchanges out of the efficiency table...?

        #  === TECH SETS ===

        # tech_resource
        raw = cur.execute("SELECT tech from main.technologies WHERE flag = 'r'").fetchall()
        load_element(M.tech_resource, raw, self.viable_techs)

        # tech_production
        raw = cur.execute("SELECT tech from main.technologies WHERE flag LIKE 'p%'").fetchall()
        load_element(M.tech_production, raw, self.viable_techs)

        # tech_baseload
        raw = cur.execute(f"SELECT tech from main.technologies where flag = 'pb'").fetchall()
        load_element(M.tech_baseload, raw, self.viable_techs)

        # tech_storage
        raw = cur.execute(f"SELECT tech from main.technologies where flag = 'ps'").fetchall()
        load_element(M.tech_storage, raw, self.viable_techs)

        # tech_reserve
        raw = cur.execute('SELECT tech from main.tech_reserve').fetchall()
        load_element(M.tech_reserve, raw, self.viable_techs)

        # tech_ramping
        techs = set()
        if self.table_exists('RampUp'):
            ramp_up_techs = cur.execute('SELECT tech from main.RampUp').fetchall()
            techs.update({t[0] for t in ramp_up_techs})
        if self.table_exists('RampDown'):
            ramp_dn_techs = cur.execute('SELECT tech from main.RampDown').fetchall()
            techs.update({t[0] for t in ramp_dn_techs})
        load_element(M.tech_ramping, sorted((t, ) for t in techs), self.viable_techs)  # sort for
        # deterministic behavior

        # tech_reserve
        raw = cur.execute('SELECT tech from main.tech_reserve').fetchall()
        load_element(M.tech_reserve, raw, self.viable_techs)

        # tech_curtailment
        raw = cur.execute('SELECT tech from main.tech_curtailment').fetchall()
        load_element(M.tech_curtailment, raw, self.viable_techs)

        # tech_rps
        # TODO:  later

        # tech_flex
        # TODO:  later

        # tech_exchange
        raw = cur.execute('SELECT tech from main.tech_exchange').fetchall()
        load_element(M.tech_exchange, raw, self.viable_techs)

        # groups & tech_groups
        # TODO:  later

        # tech_annual
        raw = cur.execute('SELECT tech from main.tech_annual').fetchall()
        load_element(M.tech_annual, raw, self.viable_techs)

        # tech_variable
        if self.table_exists('tech_variable'):
            raw = cur.execute('SELECT tech from main.tech_variable').fetchall()
            load_element(M.tech_variable, raw, self.viable_techs)

        # tech_retirement
        # TODO:  later

        #  === COMMODITIES ===

        # commodity_demand
        raw = cur.execute("SELECT comm_name FROM main.commodities WHERE flag = 'd'").fetchall()
        load_element(M.commodity_demand, raw, self.viable_comms)

        # commodity_emissions
        # currently NOT validated against anything... shouldn't be a problem ?
        raw = cur.execute("SELECT comm_name FROM main.commodities WHERE flag = 'e'").fetchall()
        load_element(M.commodity_emissions, raw)

        # commodity_physical
        raw = cur.execute("SELECT comm_name FROM main.commodities WHERE flag = 'p'").fetchall()
        # The model enforces 0 symmetric difference between the physical commodities
        # and the input commodities, so we need to include only the viable INPUTS
        load_element(M.commodity_physical, raw, self.viable_input_comms)

        #  === PARAMS ===

        # Efficiency
        if mi:
            # use what we have already computed
            raw = self.efficiency_values
        else:
            raw = cur.execute(
                'SELECT regions, input_comm, tech, vintage, output_comm, efficiency '
                'FROM main.Efficiency',
            ).fetchall()

        load_element(M.Efficiency, raw)


        # ExistingCapacity
        default_lifetime = TemoaModel.default_lifetime_tech
        if mi:
            # this is gonna be a bit ugly because we need to calculate the lifetime "on the fly"
            # or we will get warnings in later years by including things that are dead
            # noinspection SqlUnused
            raw = cur.execute(
                'SELECT region, tech, vintage, capacity FROM main.MyopicCapacity '
                '  UNION '
                'SELECT regions, tech, vintage, exist_cap FROM main.ExistingCapacity '
            ).fetchall()
        else:
            raw = cur.execute(
                'SELECT regions, tech, vintage, exist_cap FROM main.ExistingCapacity'
            ).fetchall()
        load_element(M.ExistingCapacity, raw, self.viable_rtv, (0, 1, 2))

        # GlobalDiscountRate
        raw = cur.execute('SELECT rate from main.GlobalDiscountRate').fetchall()
        # do this separately as it is non-indexed, so we need to make a mapping with None
        data[M.GlobalDiscountRate.name] = {None: raw[0][0]}

        # SegFrac
        raw = cur.execute(
            'SELECT season_name, time_of_day_name, segfrac FROM main.SegFrac'
        ).fetchall()
        load_element(M.SegFrac, raw)

        # DemandSpecificDistribution
        raw = cur.execute(
            'SELECT regions, season_name, time_of_day_name, demand_name, dds from main.DemandSpecificDistribution'
        ).fetchall()
        load_element(M.DemandSpecificDistribution, raw)

        # Demand
        raw = cur.execute(
            f'SELECT regions, periods, demand_comm, demand FROM main.Demand '
            f'WHERE {mi.base_year} <= Demand.periods AND Demand.periods <= {mi.last_demand_year}'
        ).fetchall()
        load_element(M.Demand, raw)

        # RescourceBound
        # TODO:  later

        # CapacityToActivity
        raw = cur.execute(
            'SELECT regions, tech, c2a from main.CapacityToActivity '
        ).fetchall()
        load_element(M.CapacityToActivity, raw, self.viable_techs, (1,))

        # CapacityFactorTech
        raw = cur.execute(
            'SELECT regions, season_name, time_of_day_name, tech, cf_tech '
            'from main.CapacityFactorTech'
        ).fetchall()
        load_element(M.CapacityFactorTech, raw, self.viable_techs, (3,))

        # CapacityFactorProcess
        raw = cur.execute(
            'SELECT regions, season_name, time_of_day_name, tech, vintage, cf_process '
            ' from main.CapacityFactorProcess'
        ).fetchall()
        load_element(M.CapacityFactorProcess, raw, self.viable_rtv, (0, 3, 4))

        # LifetimeTech
        raw = cur.execute('SELECT regions, tech, life FROM main.LifetimeTech').fetchall()
        load_element(M.LifetimeTech, raw, self.viable_techs, val_loc=(1,))

        # LifetimeProcess
        raw = cur.execute(
            'SELECT regions, tech, vintage, life_process FROM main.LifetimeProcess'
        ).fetchall()
        load_element(M.LifetimeProcess, raw, self.viable_rtv, val_loc=(0, 1, 2))

        # LifetimeLoanTech
        raw = cur.execute(
            'SELECT regions, tech, loan FROM main.LifetimeLoanTech'
        ).fetchall()
        load_element(M.LifetimeLoanTech, raw, self.viable_techs, (1,))

        # TechInputSplit
        raw = cur.execute(
            'SELECT regions, periods, input_comm, tech, ti_split FROM main.TechInputSplit '
            f'WHERE {mi.base_year} <= periods AND periods <= {mi.last_demand_year}'
        ).fetchall()
        load_element(M.TechInputSplit, raw, self.viable_techs, (3,))

        # TechInputSplitAverage
        if self.table_exists('TechInputSplitAverage'):
            raw = cur.execute(
                'SELECT regions, periods, input_comm, tech, ti_split '
                'FROM main.TechInputSplitAverage '
                f'WHERE {mi.base_year} <= periods AND periods <={mi.last_demand_year}'
            ).fetchall()
            load_element(M.TechInputSplitAverage, raw, self.viable_techs, (3,))

        # TechOutputSplit
        # TODO:  later

        # RenewablePortfolioStandard
        # TODO:  later

        # CostFixed
        raw = cur.execute(
            'SELECT regions, periods, tech, vintage, cost_fixed FROM main.CostFixed '
            f'WHERE {mi.base_year} <= CostFixed.periods AND CostFixed.periods <= {mi.last_demand_year}'
        ).fetchall()
        load_element(M.CostFixed, raw, self.viable_rtv, val_loc=(0, 2, 3))

        # CostInvest
        # exclude "existing" vintages by screening for base year and beyond.
        # the "viable_rtv" will filter anything beyond view
        raw = cur.execute(
            'SELECT regions, tech, vintage, cost_invest FROM main.CostInvest '
            f'WHERE {mi.base_year} <= vintage'
        ).fetchall()
        load_element(M.CostInvest, raw, self.viable_rtv, (0, 1, 2))

        # CostVariable
        raw = cur.execute(
            'SELECT regions, periods, tech, vintage, cost_variable FROM main.CostVariable '
            f'WHERE {mi.base_year} <= periods AND periods <= {mi.last_demand_year}'
        ).fetchall()
        load_element(M.CostVariable, raw, self.viable_rtv, (0, 2, 3))

        # DiscountRate
        raw = cur.execute(
            'SELECT regions, tech, vintage, tech_rate FROM main.DiscountRate '
            f'WHERE vintage >= {mi.base_year}'
        ).fetchall()
        load_element(M.DiscountRate, raw, self.viable_rtv, (0, 1, 2))

        # MinCapacity
        raw = cur.execute(
            'SELECT regions, periods, tech, mincap FROM main.MinCapacity '
            f'WHERE {mi.base_year} <= periods AND periods <= {mi.last_demand_year}'
        ).fetchall()
        load_element(M.MinCapacity, raw, self.viable_techs, (2,))

        # MaxCapacity
        raw = cur.execute(
            'SELECT regions, periods, tech, maxcap FROM main.MaxCapacity '
            f'WHERE {mi.base_year} <= periods AND periods <= {mi.last_demand_year}'
        ).fetchall()
        load_element(M.MaxCapacity, raw, self.viable_techs, (2,))

        # MinNewCap, MaxNewCap
        # TODO:  later

        # MaxResource
        raw = cur.execute(
            'SELECT regions, tech, maxres from main.MaxResource'
        ).fetchall()
        load_element(M.MaxResource, raw, self.viable_techs, (1,))

        # MaxActivity
        if self.table_exists("MaxActivity"):
            raw = cur.execute(
                'SELECT regions, periods, tech, maxact FROM main.MaxActivity '
                f'WHERE periods >= {mi.base_year} and periods <= {mi.last_demand_year}'
            ).fetchall()
            load_element(M.MaxActivity, raw, self.viable_techs, (2,))

        # MinActivity
        raw = cur.execute(
            'SELECT regions, periods, tech, minact FROM main.MinActivity '
            f'WHERE periods >= {mi.base_year} and periods <= {mi.last_demand_year}'
        ).fetchall()
        load_element(M.MinActivity, raw, self.viable_techs, (2,))

        # Min(Max)AnnualCapacityFactor
        # TODO:  later

        # GrowthRateMax
        raw = cur.execute(
            'SELECT regions, tech, growthrate_max FROM main.GrowthRateMax'
        ).fetchall()
        load_element(M.GrowthRateMax, raw, self.viable_techs, (1,))

        # GrowthRateSeed
        raw = cur.execute(
            'SELECT regions, tech, growthrate_seed FROM main.GrowthRateSeed'
        ).fetchall()
        load_element(M.GrowthRateSeed, raw, self.viable_techs, (1,))

        # EmissionLimit
        raw = cur.execute(
            'SELECT regions, periods, emis_comm, emis_limit FROM main.EmissionLimit '
            f'WHERE periods >= {mi.base_year} AND periods <= {mi.last_demand_year}'
        ).fetchall()
        load_element(M.EmissionLimit, raw, self.viable_comms, (2,))

        # EmissionActivity
        # this could be ugly too.  We can have region groups here, so for this to be valid:
        # 1.  The tech-vintage must be viable somewhere, but it may not have
        #     been built in all eligible regions in a region-group
        # 2.  The input/output commodities must be viable also
        # 3.  The emission commodities are separate
        # 4.  The vintage must be in time_optimize, so screen the early stuff and then
        #     The viable_vintages will screen the latter stuff
        # The current emission constraint screens by valid inputs, so if it is NOT
        # built in a particular region, this should still be OK
        raw = cur.execute(
            'SELECT regions, emis_comm, input_comm, tech, vintage, output_comm, emis_act '
            'FROM main.EmissionActivity '
            f'WHERE vintage >= {mi.base_year}'
        ).fetchall()
        filtered = [(r, e, i, t, v, o, val) for r, e, i, t, v, o, val in raw
                    if t in self.viable_techs
                    and v in self.viable_vintages
                    and i in self.viable_comms
                    and o in self.viable_comms]
        load_element(M.EmissionActivity, filtered)

        # Min(Max)ActivityGroup, Min(Max)ActivityGroup, Min(Max)NewCapacityGroup,
        # Min(Max)CapacityShare, Min(Max)ActivityShare
        # TODO:  later

        # LinkedTechs
        # Note:  Both of the linked techs must be viable.  As this is non period/vintage
        #        specific, it should be true that if one is built, the other is also
        raw = cur.execute(
            'SELECT primary_region, primary_tech, emis_comm, linked_tech FROM main.LinkedTechs'
        ).fetchall()
        load_element(M.LinkedTechs, raw, self.viable_techs, (1,))

        # RampUp
        if self.table_exists('RampUp'):
            raw = cur.execute(
                'SELECT regions, tech, ramp_up FROM main.RampUp'
            ).fetchall()
            load_element(M.RampUp, raw, self.viable_techs, (1,))

        # RampDown
        if self.table_exists('RampDown'):
            raw = cur.execute(
                'SELECT regions, tech, ramp_down FROM main.RampDown'
            ).fetchall()
            load_element(M.RampDown, raw, self.viable_techs, (1,))

        # CapacityCredit
        raw = cur.execute(
            'SELECT regions, periods, tech, vintage, cf_tech FROM main.CapacityCredit '
            f'WHERE periods >= {mi.base_year} AND periods <= {mi.last_demand_year}'
        ).fetchall()
        load_element(M.CapacityCredit, raw, self.viable_rtv, (0, 2, 3))

        # PlanningReserveMargin
        raw = cur.execute(
            'SELECT regions, reserve_margin FROM main.PlanningReserveMargin'
        ).fetchall()
        load_element(M.PlanningReserveMargin, raw)

        # StorageDuration
        raw = cur.execute(
            'SELECT regions, tech, duration FROM main.StorageDuration'
        ).fetchall()
        load_element(M.StorageDuration, raw, self.viable_techs)

        # StorageInit
        # TODO:  later

        # pyomo namespace format has data[namespace][idx]=value
        # the default namespace is None, thus...
        namespace = {None: data}
        if self.debugging:
            for item in namespace[None].items():
                print(item[0], item[1])
        dp = DataPortal(data_dict=namespace)
        toc = time.time()
        logger.debug('Data Portal Load time: %0.5f seconds', (toc - tic))
        return dp
