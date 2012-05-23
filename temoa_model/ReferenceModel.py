#!/usr/bin/env coopr_python

from temoa_lib import *
from temoa_model import *

def StochasticPointObjective_rule ( M, A_period ):
	"""\
Stochastic objective function.

TODO: update with LaTeX version of equation.
	"""
	l_loan_period_fraction_indices = M.LoanLifeFrac.keys()
	l_tech_period_fraction_indices = M.TechLifeFrac.keys()

	l_loan_costs = sum(
	    M.V_CapacityInvest[l_tech, l_vin]
	  * value(
	      M.PeriodRate[ A_period ].value
	    * M.CostInvest[l_tech, l_vin].value
	    * M.LoanAnnualize[l_tech, l_vin].value
	  )

	  for l_tech, l_vin in M.CostInvest.keys()
	  if (A_period, l_tech, l_vin) not in l_loan_period_fraction_indices
	  if loanIsActive( A_period, l_tech, l_vin )
	) + sum(
	    M.V_CapacityInvest[l_tech, l_vin]
	  * value(
	      M.CostInvest[l_tech, l_vin].value
	    * M.LoanAnnualize[l_tech, l_vin].value
	  )
	  * sum(
	      (1 + M.GlobalDiscountRate) ** (M.time_optimize.first() - l_per - y)
	      for y in range( 0, M.PeriodLength[ l_per ] * M.LoanLifeFrac[l_per, l_tech, l_vin])
	    )

	  for l_per, l_tech, l_vin in l_loan_period_fraction_indices
	  if l_per == A_period
	)

	l_fixed_costs = sum(
	    M.V_Capacity[l_tech, l_vin]
	  * value(
	      M.CostFixed[A_period, l_tech, l_vin].value
	    * M.PeriodRate[ A_period ].value
	  )

	  for l_per, l_tech, l_vin in M.CostFixed.keys()
	  if l_per == A_period
	  if (l_per, l_tech, l_vin) not in l_tech_period_fraction_indices
	) + sum(
	    M.V_CapacityInvest[l_tech, l_vin]
	  * M.CostInvest[l_tech, l_vin].value
	  * M.LoanAnnualize[l_tech, l_vin].value
	  * sum(
	      (1 + M.GlobalDiscountRate) ** (M.time_optimize.first() - l_per - y)
	      for y in range( 0, M.PeriodLength[ l_per ] * M.TechLifeFrac[l_per, l_tech, l_vin])
	    )

	  for l_per, l_tech, l_vin in l_tech_period_fraction_indices
	  if l_per == A_period
	  if (l_per, l_tech, l_vin) in M.CostFixed.keys()
	)

	l_marg_costs = sum(
	    M.V_Activity[A_period, l_season, l_time_of_day, l_tech, l_vin]
	  * M.PeriodRate[ A_period ]
	  * M.CostMarginal[A_period, l_tech, l_vin]

	  for l_per, l_tech, l_vin in M.CostMarginal.keys()
	  if l_per == A_period
	  if (l_per, l_tech, l_vin) not in l_tech_period_fraction_indices
	  for l_season in M.time_season
	  for l_time_of_day in M.time_of_day
	)

	l_cost = (l_loan_costs + l_fixed_costs + l_marg_costs)

	expr = (M.StochasticPointCost[ A_period ] == l_cost)
	return expr

def Objective_rule ( M ):
	return sum( M.StochasticPointCost[ pp ] for pp in M.time_optimize )

M = model = temoa_create_model( 'TEMOA Stochastic' )

M.StochasticPointCost = Var( M.time_optimize, within=NonNegativeReals )
M.StochasticPointCostConstraint = Constraint( M.time_optimize, rule=StochasticPointObjective_rule )

M.TotalCost = Objective( rule=Objective_rule, sense=minimize )