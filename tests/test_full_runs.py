"""
Test a couple full-runs to match objective function value and some internals

Written by:  J. F. Hyink
jeff@westernspark.us
https://westernspark.us
Created on:  6/27/23

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
"""

import logging
import pathlib

import pyomo.environ as pyo
import pytest
from pyomo.core import Constraint, Var

from definitions import PROJECT_ROOT

# from src.temoa_model.temoa_model import temoa_create_model
from temoa.temoa_model.temoa_sequencer import TemoaSequencer, TemoaMode
from tests.legacy_test_values import TestVals, test_vals

logger = logging.getLogger(__name__)
# list of test scenarios for which we have captured results in legacy_test_values.py
legacy_config_files = [
    {'name': 'utopia', 'filename': 'config_utopia.toml'},
    {'name': 'test_system', 'filename': 'config_test_system.toml'},
]


@pytest.mark.parametrize(
    'system_test_run',
    argvalues=legacy_config_files,
    indirect=True,
    ids=[d['name'] for d in legacy_config_files],
)
def test_against_legacy_outputs(system_test_run):
    """
    This test compares tests of legacy models to captured test results
    """
    data_name, res, mdl = system_test_run
    logger.info('Starting output test on scenario: %s', data_name)
    expected_vals = test_vals.get(data_name)  # a dictionary of expected results

    # inspect some summary results
    assert res['Solution'][0]['Status'] == 'optimal'
    assert res['Solution'][0]['Objective']['TotalCost']['Value'] == pytest.approx(
        expected_vals[TestVals.OBJ_VALUE], 0.00001
    )

    # inspect a couple set sizes
    efficiency_param: pyo.Param = mdl.Efficiency
    # check the set membership
    assert (
            len(tuple(efficiency_param.sparse_iterkeys())) == expected_vals[TestVals.EFF_INDEX_SIZE]
    ), 'should match legacy numbers'

    # check the size of the domain.  NOTE:  The build of the domain here may be "expensive" for large models
    assert (
            len((efficiency_param.index_set().domain)) == expected_vals[TestVals.EFF_DOMAIN_SIZE]
    ), 'should match legacy numbers'

    # inspect the total variable and constraint counts
    # gather some stats...
    c_count = 0
    v_count = 0
    for constraint in mdl.component_objects(ctype=Constraint):
        c_count += len(constraint)
    for var in mdl.component_objects(ctype=Var):
        v_count += len(var)

    # check the count of constraints & variables
    assert c_count == expected_vals[TestVals.CONSTR_COUNT], 'should have this many constraints'
    assert v_count == expected_vals[TestVals.VAR_COUNT], 'should have this many variables'


@pytest.mark.skip(reason='Myopic test on hold till myopic is running again')
def test_myopic_utopia():
    """
    test the myopic functionality on Utopia.  We need to copy the source db to make the output and then erase
    it because re-runs with the same output db are not possible....get "UNIQUE" errors in db on 2nd run

    We will use the output target in the config file for this test as a shortcut to make/remove the database

    This test will change after conversion of temoa_myopic.py.  RN, it is a good placeholder

    """
    # eps = 1e-3
    # config_file = pathlib.Path(PROJECT_ROOT, 'tests', 'testing_configs', 'config_sample')
    # # config_file = pathlib.Path(PROJECT_ROOT, 'tests', 'testing_configs', 'config_utopia_myopic')
    # input_db = pathlib.Path(PROJECT_ROOT, 'tests', 'testing_data', 'temoa_utopia.sqlite')
    # output_db = pathlib.Path(PROJECT_ROOT, 'tests', 'testing_outputs', 'temoa_utopia_output_catcher.sqlite')
    # if os.path.isfile(output_db):
    #     os.remove(output_db)
    # shutil.copy(input_db, output_db)  # put a new copy in place, ones that are used before fail.
    # model = TemoaModel()
    # temoa_solver = TemoaSolver(model, config_filename=config_file)
    # for _ in temoa_solver.createAndSolve():
    #     pass
    # # inspect the output db for results
    # con = sqlite3.connect(output_db)
    # cur = con.cursor()
    # query = "SELECT t_periods, emissions FROM Output_Emissions WHERE tech is 'IMPDSL1'"
    # emission = cur.execute(query).fetchall()
    #
    # # The emissions for diesel are present in each year and should be a good proxy for comparing
    # # results
    # diesel_emissions_by_year = {y: e for (y, e) in emission}
    # assert abs(diesel_emissions_by_year[1990] - 2.8948) < eps
    # assert abs(diesel_emissions_by_year[2000] - 2.4549) < eps
    # assert abs(diesel_emissions_by_year[2010] - 5.4539) < eps
    # os.remove(output_db)

# TODO:  add additional tests for myopic that have retirement eligible things in them
