# Sensitivity Engine Specification

## Overview

The Sensitivity Engine generates 2-way sensitivity matrices showing how key metrics vary across combinations of input assumptions. This is essential for IC packages and risk assessment.

## Core Concepts

### 2-Way Matrix

A 2-way matrix varies two inputs simultaneously and displays the resulting metric at each intersection:

```
                    Exit Cap Rate
                  4.5%   5.0%   5.5%   6.0%
Rent        2%   18.5%  16.2%  14.3%  12.7%
Growth      3%   20.1%  17.8%  15.9%  14.2%
Rate        4%   21.8%  19.4%  17.4%  15.7%
            5%   23.4%  21.0%  18.9%  17.2%
```

### Common Sensitivity Pairs

1. **Exit Cap × Rent Growth** (most common)
   - Row: Rent growth rates (2%, 3%, 4%, 5%)
   - Column: Exit cap rates (4.5%, 5.0%, 5.5%, 6.0%)
   - Metric: Levered IRR

2. **Exit Cap × Purchase Price**
   - Row: Purchase price variance (-5%, 0%, +5%, +10%)
   - Column: Exit cap rates
   - Metric: Equity Multiple

3. **Interest Rate × LTV**
   - Row: Interest rates (5%, 6%, 7%, 8%)
   - Column: LTV ratios (60%, 65%, 70%, 75%)
   - Metric: DSCR

### Supported Metrics

- Levered IRR
- Unlevered IRR
- Levered Equity Multiple
- Unlevered Equity Multiple
- Average DSCR
- Minimum DSCR
- Cash-on-Cash Yield (Year 1)
- Going-In Cap Rate
- Exit NOI Yield

## Input Schema

### Sensitivity Configuration

```json
{
  "sensitivity_config": {
    "row_input": {
      "parameter": "rent_growth_rate",
      "values": [0.02, 0.03, 0.04, 0.05],
      "labels": ["2%", "3%", "4%", "5%"]
    },
    "column_input": {
      "parameter": "exit_cap_rate",
      "values": [0.045, 0.050, 0.055, 0.060],
      "labels": ["4.5%", "5.0%", "5.5%", "6.0%"]
    },
    "metrics": ["levered_irr", "levered_em", "average_dscr"],
    "base_case": {
      "row_index": 1,
      "column_index": 1
    }
  }
}
```

### Field Definitions

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| row_input.parameter | string | Yes | Parameter to vary on rows |
| row_input.values | array | Yes | Values to test |
| row_input.labels | array | No | Display labels (defaults to values) |
| column_input.parameter | string | Yes | Parameter to vary on columns |
| column_input.values | array | Yes | Values to test |
| column_input.labels | array | No | Display labels |
| metrics | array | Yes | Which metrics to calculate |
| base_case | object | No | Index of base case for highlighting |

### Supported Parameters

| Parameter | Maps To | Notes |
|-----------|---------|-------|
| rent_growth_rate | growth_assumptions.annual_growth_rate | Annual compound rate |
| exit_cap_rate | exit_assumptions.exit_cap_rate | Terminal cap rate |
| purchase_price | purchase_assumptions.purchase_price | Absolute or % variance |
| interest_rate | debt_terms.rate | Annual interest rate |
| ltv_ratio | Derived from debt/price | Loan-to-value |
| vacancy_rate | physical_vacancy_curve[*].vacancy_rate | Stabilized vacancy |
| opex_growth_rate | opex_table[*].growth_rate | Expense growth |

## Output Schema

### Sensitivity Matrix

```json
{
  "sensitivity_results": {
    "levered_irr": {
      "metric_name": "Levered IRR",
      "row_labels": ["2%", "3%", "4%", "5%"],
      "column_labels": ["4.5%", "5.0%", "5.5%", "6.0%"],
      "matrix": [
        [0.185, 0.162, 0.143, 0.127],
        [0.201, 0.178, 0.159, 0.142],
        [0.218, 0.194, 0.174, 0.157],
        [0.234, 0.210, 0.189, 0.172]
      ],
      "base_case_value": 0.178,
      "base_case_position": [1, 1],
      "min_value": 0.127,
      "max_value": 0.234,
      "variance_from_base": {
        "min_delta": -0.051,
        "max_delta": 0.056
      }
    }
  }
}
```

### Summary Statistics

```json
{
  "summary": {
    "total_scenarios": 16,
    "computation_time_ms": 1250,
    "base_case_metrics": {
      "levered_irr": 0.178,
      "levered_em": 2.15,
      "average_dscr": 1.42
    },
    "worst_case": {
      "levered_irr": 0.127,
      "scenario": "2% rent growth, 6.0% exit cap"
    },
    "best_case": {
      "levered_irr": 0.234,
      "scenario": "5% rent growth, 4.5% exit cap"
    }
  }
}
```

## Calculation Logic

### Matrix Generation

```python
def generate_sensitivity_matrix(
    base_deal: Dict,
    row_param: str,
    row_values: List[float],
    col_param: str,
    col_values: List[float],
    metric: str,
) -> List[List[float]]:
    """
    Generate 2D matrix of metric values.
    """
    matrix = []
    for row_val in row_values:
        row = []
        for col_val in col_values:
            # Create modified deal
            modified = apply_overrides(base_deal, {
                row_param: row_val,
                col_param: col_val,
            })
            # Run full calculation
            result = run_full_model(modified)
            # Extract metric
            row.append(extract_metric(result, metric))
        matrix.append(row)
    return matrix
```

### Parameter Application

Each parameter maps to a specific location in the deal structure:

```python
def apply_overrides(deal: Dict, overrides: Dict) -> Dict:
    """
    Apply parameter overrides to deal structure.
    """
    modified = deep_copy(deal)

    for param, value in overrides.items():
        if param == "rent_growth_rate":
            modified["growth_assumptions"]["annual_growth_rate"] = value
        elif param == "exit_cap_rate":
            modified["exit_assumptions"]["exit_cap_rate"] = value
        elif param == "purchase_price":
            modified["purchase_assumptions"]["purchase_price"] = value
        elif param == "interest_rate":
            modified["debt_terms"]["rate"] = value
        # ... additional mappings

    return modified
```

### Metric Extraction

```python
def extract_metric(result: Dict, metric: str) -> float:
    """
    Extract specific metric from model results.
    """
    metrics = result["metrics"]

    if metric == "levered_irr":
        return metrics["irr"]["levered_irr"]
    elif metric == "unlevered_irr":
        return metrics["irr"]["unlevered_irr"]
    elif metric == "levered_em":
        return metrics["equity_multiple"]["levered_em"]
    elif metric == "average_dscr":
        return metrics["dscr"]["average_dscr"]
    # ... additional metrics
```

## Usage Example

### Basic Exit Cap × Rent Growth

```python
from engine.modules.sensitivity import run_sensitivity_analysis

config = {
    "row_input": {
        "parameter": "rent_growth_rate",
        "values": [0.02, 0.03, 0.04, 0.05],
    },
    "column_input": {
        "parameter": "exit_cap_rate",
        "values": [0.045, 0.050, 0.055, 0.060],
    },
    "metrics": ["levered_irr", "levered_em"],
}

results = run_sensitivity_analysis(deal_inputs, config)

# Access matrix
irr_matrix = results["sensitivity_results"]["levered_irr"]["matrix"]
```

### Custom Variance Analysis

```python
# Purchase price variance (-10% to +10%)
config = {
    "row_input": {
        "parameter": "purchase_price_variance",
        "values": [-0.10, -0.05, 0.0, 0.05, 0.10],
        "labels": ["-10%", "-5%", "Base", "+5%", "+10%"],
    },
    "column_input": {
        "parameter": "exit_cap_rate",
        "values": [0.050, 0.055, 0.060],
    },
    "metrics": ["levered_irr"],
}
```

## Performance Considerations

### Scenario Count

- Total scenarios = rows × columns × metrics
- Typical: 4 × 4 × 3 = 48 calculations
- Maximum recommended: 10 × 10 × 5 = 500 calculations

### Optimization Strategies

1. **Caching**: Cache intermediate results (time grid, base curves)
2. **Parallel execution**: Run independent scenarios concurrently
3. **Delta calculation**: Only recalculate affected modules

### Estimated Runtime

| Scenarios | Estimated Time |
|-----------|---------------|
| 16 | < 1 second |
| 64 | 1-2 seconds |
| 256 | 5-10 seconds |
| 500+ | 15-30 seconds |

## Future Enhancements (v0.3+)

### Monte Carlo Simulation

Instead of discrete scenarios, sample from distributions:

```json
{
  "monte_carlo_config": {
    "iterations": 10000,
    "distributions": {
      "rent_growth_rate": {
        "type": "normal",
        "mean": 0.03,
        "std_dev": 0.01
      },
      "exit_cap_rate": {
        "type": "triangular",
        "min": 0.045,
        "mode": 0.055,
        "max": 0.070
      }
    },
    "outputs": ["irr_distribution", "probability_metrics"]
  }
}
```

### Tornado Charts

Single-variable sensitivity showing impact ranking:

```
Impact on IRR:
Exit Cap    ████████████████ ± 4.2%
Rent Growth ██████████████   ± 3.5%
LTV         ████████         ± 1.8%
OpEx Growth ████             ± 0.9%
```

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 0.1 | 2026-01 | Initial specification (2-way matrix only) |
| Future | - | Monte Carlo, tornado charts |
