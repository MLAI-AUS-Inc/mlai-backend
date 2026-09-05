# HealthHack scoring data

The HealthHack scoring algorithm remains in `hospital/views.py`, but its
held-out ground-truth CSV is not stored in this public repository. The current
solution and archived evaluation data are owned by the private
[`MLAI-AUS-Inc/healthhack-scoring`](https://github.com/MLAI-AUS-Inc/healthhack-scoring)
repository.

## Runtime contract

Provision `data/current/solution.csv` from the private repository onto the
backend filesystem and set `HEALTH_HACK_SOLUTION_PATH` to its absolute path.
The file must have this exact header:

```csv
ID,predicted_label,Usage
```

IDs must be contiguous from 1, labels must be integers from 0 through 3, and
usage values must be either `Public` or `Private`. The backend validates the
entire file before accepting it and refuses to score when the setting is empty,
the file is absent, or its contents violate the contract.

Do not copy the solution into an image layer, public build artifact, log, test
fixture, or pull request. Provisioning must preserve the private repository's
access controls.
