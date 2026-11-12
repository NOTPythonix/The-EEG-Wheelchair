# --- Code from OpenChamber (Deepseek V4.1 Flash 1017) ---
import pickle
import numpy as np

# eeg_data.pkl holds an EEGData object, so this class must exist before unpickling
class EEGData:
    def __init__(self, data, labels, ch_names, sampling_rate, subject_ids):
        self.data = data
        self.labels = labels
        self.ch_names = ch_names
        self.sampling_rate = sampling_rate
        self.subject_ids = subject_ids

with open("eeg_data.pkl", "rb") as f:
    eeg_data = pickle.load(f)

labels = eeg_data.labels
data = eeg_data.data
subject_ids = np.asarray(eeg_data.subject_ids)

RANDOM_SEED = 67
rng = np.random.default_rng(RANDOM_SEED)
datasets_per_subject = {}

for subject_id in np.unique(subject_ids):
    idx = rng.permutation(np.flatnonzero(subject_ids == subject_id))
    split_index = int(len(idx) * 0.8)

    train_mask = np.zeros(len(labels), dtype=bool)
    test_mask = np.zeros(len(labels), dtype=bool)
    train_mask[idx[:split_index]] = True
    test_mask[idx[split_index:]] = True

    # float64: TangentSpace's matrix log wants it
    datasets_per_subject[subject_id] = [
        data[train_mask].astype(np.float64),
        labels[train_mask].flatten(),
        data[test_mask].astype(np.float64),
        labels[test_mask].flatten(),
    ]

from sklearn.pipeline import make_pipeline
from sklearn.svm import SVC
from sklearn.model_selection import GridSearchCV, StratifiedKFold
from pyriemann.estimation import Covariances
from pyriemann.tangentspace import TangentSpace
from moabb.pipelines.features import AugmentedDataset

pipe = make_pipeline(
    AugmentedDataset(order=3, lag=2),
    Covariances(estimator="cov"),
    TangentSpace(metric="riemann"),
    SVC(kernel="rbf"),
)

param_grid = {
    "augmenteddataset__order": list(range(1, 11)),
    "augmenteddataset__lag":   list(range(1, 11)),
    "svc__C":      [0.5, 1.0, 1.5],
    "svc__kernel": ["rbf", "linear"],
}   # 600 configs

acc_per_subject = {}
for subject_id in np.unique(subject_ids):
    X_train, y_train, X_test, y_test = datasets_per_subject[subject_id]
    print(f"Running ACM+TS+SVM for subject {subject_id}...")

    gs = GridSearchCV(
        pipe, param_grid,
        cv=StratifiedKFold(3, shuffle=True, random_state=RANDOM_SEED),
        scoring="accuracy", n_jobs=-1,
        error_score="raise",
    )
    gs.fit(X_train, y_train)

    acc = gs.score(X_test, y_test)
    acc_per_subject[subject_id] = acc
    print(f"  best params: {gs.best_params_}")
    print(f"Test Accuracy for subject {subject_id}: {acc:.4f}")

print(f"Average accuracy across all subjects: "
      f"{np.mean(list(acc_per_subject.values())):.4f}")
# --- ---