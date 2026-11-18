# QUBO VALIDATOR

This project is a graphical interface to extract different information from a QUBO formulation.

The main goal of this app, is to allow a research to effectively and quickly analyze the impact of a QUBO formulation
when dealing with an optimization problem. To do that, it must retrieve user data, process it and return statistics.

## Inputs

For this program, the inputs are a combination of mathematical formulas extracted from different input boxes, parameters for optimization and conditional values from input radios. 

Overall, the data are:

- **QUBO**: an input string that must be serialized and transformed into a mathematical object that can be used for optimization.
- **penalties**: QUBO formulation penalties. Must be another input string from a different textbox. It also requires to be serialized and transformed for later evaluation.
- **optimizers**: A list used to select for the optimizers and backends available for optimization. The current optimizers allowed are:
    - `ExactSolver`
    - `SimulatedAnnealingSampler`
    - `RandomSampler`
    All from DWave Ocean SDK.
- **QUBO Type**: A selector for `binary` or `ising` formulation.
- **Conditional Parameters**: Extra parameters for the current selected optimizer.

## Features

The app feature set consists in a list of statistics that it must show to the user.

### 1. How many qubits are required for the current QUBO.

For this requirement, the user will input the formula and the system must read it and count how many variables in the form $x$ or $s$ are used,
then output the expected amount of qubits required for the quantum implementation.

Some formulas may come in the form of an inequality, which requires `slack variables`, these are needed to be counted as well.

We would like for the user to add `penalties` in a different box, so they can be evaluated differently.

For the user, it should be shown all this information, such as:

```
----------Amount of qubits----------
total: <<slack>+<individual>+<for penalties>>
total slack: <slack>
individual variables: <individual>
for penalties: <if any additional variable is used for penalties>
```

### 2. What's the energy landscape it generates for optimization.

Given the QUBO and penalty inputs, the system must evaluates generate a matrix corresponding to each energy value for each bit-string. Then, 
on the GUI, it must be plot a heatmap showing the values with `cbar` points indicating the lowest and highest points marked in a legend.

The plots are made using matplotlib and its machinery.

### 3. Lowest energy

The QUBO must be evaluated using the selected optimizer and a list must be given in the `ascending order of energy`, it's also required to mark the bit-strings with slowest energy with a different color. An example can be seen below:

```

+--------------+--------------+
+ bit-string   + energy value +
+--------------+--------------+
+ bit-string 0 + lowest 1     +
+ bit-string 1 + lowest 1     +
+ bit-string 2 + lowest 1     +
+ bit-string 3 + lowest 2     +
+ bit-string 4 + lowest 3     +
+ ...          + ...          +
+--------------+--------------+

```

> Some instances can lead to same energy, so keep the sequence as ascending always

### 4. Connectivity graph

The QUBO itself, no penalties, must be converted into a graph that can be visualized on the GUI. It can be done using `rustworkx` and the results from `dimod`.

### 5. Penalty Validation

The Lagrangian value for penalties must be validated by showing the range of values (min-max) the pure QUBO can hold and then it the choice of the Lagrangian term is enough to penalize terms.

### 6. Download data

From each experiment, a button on the top of the page must be shown for the user to download the project in a zip format containing:

1. the energy data in CSV (exported via `pandas`)
2. the plots in png format
3. a JSON file holding the parameters and the files names (CSV and images)

## Technologies

For this project, the whole GUI must be constructed using `PyQT`. Along with it, `DWaveOCeanSDK` for optimization and formulation, `rustworkx`
for graphs, `pandas` for managing the outputs and exporting csv files, `matplotlib` for plots, `numpy`, `sympy` and `scipy` for mathematical stuff.


## Disposition of elements

The application must be simple. 

The GUI must contain only two sections, being side by side:

- the `right` one, must be static holding the inputs and parameters of which the user must provide. It's not scrollable and when the execution is done for a given input, it must appear dynamically a button to download the project. It must have a button to submit the project for evaluation.
- the `left` one, is dynamic being blank with a text on the middle saying `waiting for input.` while no input was submitted. After submitting, it must change to a loading spinner and then all statistics must appear on a vertically scrollable container.

> the downloaded filename must be in the format: project_QUBO-<date>.zip
