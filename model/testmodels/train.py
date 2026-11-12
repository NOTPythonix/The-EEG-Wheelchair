# Code overall adapted from https://github.com/Dingkun0817/EEG-FM-Benchmark
import numpy as np
import pickle
import time
import torch
from torch.utils.data import DataLoader, TensorDataset

# EEGData object
class EEGData:
    def __init__(self, data, labels, ch_names, sampling_rate, subject_ids):
        self.data = data
        self.labels = labels
        self.ch_names = ch_names
        self.sampling_rate = sampling_rate
        self.subject_ids = subject_ids

RANDOM_SEED = 67
np.random.seed(RANDOM_SEED)
torch.manual_seed(RANDOM_SEED)

# Load BNCI2014004 data (code adapted from https://github.com/Dingkun0817/EEG-FM-Benchmark/blob/main/datasets/BNCI2014004/Preprocess_Dataset.py)
from moabb.datasets import BNCI2014_001
from moabb.paradigms import MotorImagery
def download_data():
    dataset = BNCI2014_001()
    paradigm = MotorImagery(fmin=0, fmax=120)
    alldata = paradigm.get_data(dataset)
    data, labels_string, metadata = alldata
    sessions = metadata["session"].values

    train_mask = sessions == "1test"
    data = data[train_mask, :, :]
    labels_string = [labels_string[i] for i in np.where(train_mask)[0]]

    # Get subject IDs
    subject_ids = metadata.loc[train_mask, "subject"].to_numpy()

    # channel names and sampling rate
    # -- Code from OpenChamber (Deepseek V4.1 Flash 1017) ---
    ch_names = [
    "Fz", "FC3", "FC1", "FCz", "FC2", "FC4",
    "C5", "C3", "C1", "Cz", "C2", "C4", "C6",
    "CP3", "CP1", "CPz", "CP2", "CP4",
    "P1", "Pz", "P2", "POz",
    ]
    # --- ---
    sampling_rate = 250

    # Label mapping
    label_map = {
        "left_hand": 0,
        "right_hand": 1,
        "feet": 2,
        "tongue": 3
    }

    # Convert labels to integers
    labels = np.array([label_map[label] for label in labels_string])

    # Create EEGData object
    eeg_data = EEGData(data, labels, ch_names, sampling_rate, subject_ids)

    # Save EEGData object to a pickle file
    with open("eeg_data.pkl", "wb") as f:
        pickle.dump(eeg_data, f)


def load_data():
    # Load EEGData object from pickle file
    with open("eeg_data.pkl", "rb") as f:
        eeg_data = pickle.load(f)
    return eeg_data

print("Checking if data is already downloaded...")
try:
    eeg_data = load_data()
    print("Data already downloaded, loaded successfully.")
except FileNotFoundError:
    print("Data not found. Downloading...")
    download_data()
    eeg_data = load_data()
    print("Data downloaded and loaded successfully.")

# Make dataloaders
labels = eeg_data.labels
data = eeg_data.data

num_samples = len(eeg_data.labels)

subject_ids = np.asarray(eeg_data.subject_ids)
rng = np.random.default_rng(RANDOM_SEED)

datasets_per_subject = {}

for subject_id in np.unique(subject_ids):
    train_mask = np.zeros(num_samples, dtype=bool)
    test_mask = np.zeros(num_samples, dtype=bool)
    subject_indices = np.flatnonzero(subject_ids == subject_id)
    subject_indices = rng.permutation(subject_indices)

    split_index = int(len(subject_indices) * 0.8)

    train_indices = subject_indices[:split_index]
    test_indices = subject_indices[split_index:]

    train_mask[train_indices] = True
    test_mask[test_indices] = True

    train_data, train_labels = data[train_mask], labels[train_mask]
    test_data, test_labels = data[test_mask], labels[test_mask]

    train_labels, test_labels = train_labels.flatten(), test_labels.flatten()

    train_data_tensor = torch.tensor(train_data, dtype=torch.float32)
    test_data_tensor = torch.tensor(test_data, dtype=torch.float32)

    train_labels_tensor = torch.tensor(train_labels, dtype=torch.long)
    test_labels_tensor = torch.tensor(test_labels, dtype=torch.long)
    datasets_per_subject[subject_id] = [train_data_tensor, train_labels_tensor, test_data_tensor, test_labels_tensor]

device = torch.device("cpu")

def save_model(model, path):
    torch.save(model.state_dict(), path)



def load_model(subject_id):
    checkpoint = torch.load(f"neurogpt_trained_linear_subject_{subject_id}.pth", map_location=device)
    model.load_state_dict(checkpoint)
    model.eval()

def evaluate_model(model, test_loader, subject_id):
    with torch.no_grad():
        correct = 0
        total = 0
        for datapoint, labels in test_loader:
            outputs = model(datapoint)
            _, predicted = torch.max(outputs.data, 1)
            total += labels.size(0)
            correct += (predicted == labels).sum().item()
        accuracy = correct / total
        print(f"Test Accuracy for subject {subject_id}: {accuracy:.4f}")
        return accuracy

# Getting the model (code adapted from GitHub Copilot)
from Model_NeuroGPT import EEGConformer
class NeuroGPTClassifier(torch.nn.Module):
    def __init__(self, input_channels, num_time_points, num_classes):
        super().__init__()

        self.channel_projection = torch.nn.Conv1d(in_channels=input_channels, out_channels=22, kernel_size=1)

        self.encoder = EEGConformer(n_outputs=num_classes, n_channels=22, n_times=num_time_points, is_decoding_mode=True)

        feature_dim = 2440
        self.classifier = torch.nn.Linear(
            in_features=feature_dim, out_features=num_classes
        )

        # load pretrained NeuroGPT
        checkpoint = torch.load("neurogpt.pth", map_location=device, weights_only=False)
        encoder_state_dict = {}
        for key, value in checkpoint.items():
            if key.startswith("encoder."):
                new_key = key[len("encoder."):]
                encoder_state_dict[new_key] = value
        self.encoder.load_state_dict(encoder_state_dict, strict=False)

        # Freeze the encoder
        for parameter in self.encoder.parameters():
            parameter.requires_grad = False

        # Keep channel projection trainable
        for parameter in self.channel_projection.parameters():
            parameter.requires_grad = True

        # Keep classifier trainable
        for parameter in self.classifier.parameters():
            parameter.requires_grad = True

    def forward(self, x):
        x = self.channel_projection(x)
        x = self.encoder(x)
        x = x.flatten(start_dim=1)
        x = self.classifier(x)
        return x

model = NeuroGPTClassifier(input_channels=eeg_data.data.shape[1], num_time_points=eeg_data.data.shape[2], num_classes=4)



optimizer = torch.optim.Adam([*model.channel_projection.parameters(), *model.classifier.parameters()], lr=0.003, weight_decay=0.1, eps=1e-8)
loss_function = torch.nn.CrossEntropyLoss()


def train_model(model, epochs, data, val_data):
    patience_counter = 0
    def early_stopping(val_loss, best_val_loss, patience):
        if val_loss < best_val_loss:
            nonlocal patience_counter
            patience_counter = 0
            return False
        else:
            patience_counter += 1
            if patience_counter >= patience:
                return True
            else:
                return False
    best_val_loss = 67676767676767676767676767676767
    best_epoch = 0
    for epoch in range(epochs):
        start_time_full = time.perf_counter()
        total_loss = 0.0
        time_per_piece = {
            "zero_grad": 0.0,
            "forward": 0.0,
            "loss": 0.0,
            "backward": 0.0,
            "step": 0.0,
            "loss_item": 0.0
        }
        model.train()
        for datapoint, labels in data:
            start_time = time.perf_counter()
            optimizer.zero_grad()
            end_time = time.perf_counter()
            time_per_piece["zero_grad"] += end_time - start_time
            start_time = time.perf_counter()
            outputs = model(datapoint)
            end_time = time.perf_counter()
            time_per_piece["forward"] += end_time - start_time
            start_time = time.perf_counter()
            loss = loss_function(outputs, labels)
            end_time = time.perf_counter()
            time_per_piece["loss"] += end_time - start_time
            start_time = time.perf_counter()
            loss.backward()
            end_time = time.perf_counter()
            time_per_piece["backward"] += end_time - start_time
            start_time = time.perf_counter()
            optimizer.step()
            end_time = time.perf_counter()
            time_per_piece["step"] += end_time - start_time
            start_time = time.perf_counter()
            total_loss += loss.item()
            end_time = time.perf_counter()
            time_per_piece["loss_item"] += end_time - start_time
        avg_loss = total_loss / len(train_loader)
        # Compute validation loss
        model.eval()
        val_loss = 0.0
        with torch.no_grad():
            for datapoint, labels in val_data:
                outputs = model(datapoint)
                loss = loss_function(outputs, labels)
                val_loss += loss.item()
        avg_val_loss = val_loss / len(val_data)
        if early_stopping(avg_val_loss, best_val_loss, patience=10):
                    print(f"Early stopping at epoch {epoch + 1}")
                    break
        # Best val loss save
        if avg_val_loss < best_val_loss:
            best_epoch = epoch + 1
            best_val_loss = avg_val_loss
            torch.save(model.state_dict(), f"best_model_epoch_{best_epoch}.pth")

        print(f"Epoch {epoch + 1}/{epochs}, Loss: {avg_loss:.4f}, Validation Loss: {avg_val_loss:.4f}")
        end_time_full = time.perf_counter()
        total_time = end_time_full - start_time_full
        # print(f"Total time: {total_time:.4f} seconds")
        # print(f"Total time per piece: {sum(time_per_piece.values()):.4f} seconds")
        # percent per piece
        # for key, value in time_per_piece.items():
            # print(f"{key}: {value:.4f} seconds, {value / total_time * 100:.2f}%")
        # print(f"Remaining time: {total_time - sum(time_per_piece.values()):.4f} seconds, {(total_time - sum(time_per_piece.values())) / total_time * 100:.2f}%")
        # All percents should sum to 100 (time per piece + remaining time)
        # print(f"Total percent: {sum(time_per_piece.values()) / total_time * 100 + (total_time - sum(time_per_piece.values())) / total_time * 100:.2f}%")
    print(f"Saving model with best val loss (epoch {best_epoch})")
    model.load_state_dict(torch.load(f"best_model_epoch_{best_epoch}.pth"))

acc_per_subject = {}
for subject_id in np.unique(subject_ids):
    print(f"Training for subject {subject_id}...")
    train_data_tensor, train_labels_tensor, test_data_tensor, test_labels_tensor = datasets_per_subject[subject_id]
    train_data_tensor = train_data_tensor.to(device)
    train_labels_tensor = train_labels_tensor.to(device)
    test_data_tensor = test_data_tensor.to(device)
    test_labels_tensor = test_labels_tensor.to(device)

    train_dataset = TensorDataset(train_data_tensor, train_labels_tensor)
    test_dataset = TensorDataset(test_data_tensor, test_labels_tensor)

    train_loader = DataLoader(train_dataset, batch_size=32, shuffle=True)
    test_loader = DataLoader(test_dataset, batch_size=32, shuffle=False)

    model = NeuroGPTClassifier(input_channels=eeg_data.data.shape[1], num_time_points=eeg_data.data.shape[2], num_classes=4)
    model.to(device)

    optimizer = torch.optim.Adam([*model.channel_projection.parameters(), *model.classifier.parameters()], lr=0.003, weight_decay=0.1, eps=1e-8)
    loss_function = torch.nn.CrossEntropyLoss()

    train_model(model, epochs=67, data=train_loader, val_data=test_loader)
    save_model(model, f"neurogpt_trained_linear_subject_{subject_id}.pth")
    load_model(subject_id)
    acc = evaluate_model(model, test_loader, subject_id)
    acc_per_subject[subject_id] = acc

print(f"Average accuracy across all subjects: {np.mean(list(acc_per_subject.values())):.4f}")