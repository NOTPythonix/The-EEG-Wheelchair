# Code overall adapted from https://github.com/Dingkun0817/EEG-FM-Benchmark
import numpy as np
import pickle
import time
from sklearn.metrics import balanced_accuracy_score
import torch
from torch.utils.data import DataLoader, TensorDataset

# EEGData object
class EEGData:
    def __init__(self, data, labels, ch_names, sampling_rate):
        self.data = data
        self.labels = labels
        self.ch_names = ch_names
        self.sampling_rate = sampling_rate

RANDOM_SEED = 67
np.random.seed(RANDOM_SEED)
torch.manual_seed(RANDOM_SEED)

def load_data():
    data = np.load("data/four_class_clench_trials_from_pause.npz")
    chan_names  = ["CP3", "Fp2", "Fp1", "Cz", "FC6", "FC5", "C1", "C2"]
    labels = data["class_types"]
    sampling_rate = data["sr_decl"]
    eeg_data = EEGData(
        data=data["8-30"],
        labels=labels,
        ch_names=chan_names,
        sampling_rate=sampling_rate,
    )
    return eeg_data

eeg_data = load_data()

# Make dataloaders
labels = eeg_data.labels
data = eeg_data.data

num_samples = len(eeg_data.labels)

rng = np.random.default_rng(RANDOM_SEED)
idx = rng.permutation(num_samples)

train_mask = np.zeros(num_samples, dtype=bool)
test_mask = np.zeros(num_samples, dtype=bool)

split_index = int(num_samples * 0.8)

train_indices = idx[:split_index]
test_indices = idx[split_index:]

train_mask[train_indices] = True
test_mask[test_indices] = True

train_data, train_labels = data[train_mask], labels[train_mask]
test_data, test_labels = data[test_mask], labels[test_mask]

train_labels, test_labels = train_labels.flatten(), test_labels.flatten()

train_data_tensor = torch.tensor(train_data, dtype=torch.float32)
test_data_tensor = torch.tensor(test_data, dtype=torch.float32)

train_labels_tensor = torch.tensor(train_labels, dtype=torch.long)
test_labels_tensor = torch.tensor(test_labels, dtype=torch.long)
dataset = [train_data_tensor, train_labels_tensor, test_data_tensor, test_labels_tensor]

device = torch.device("cpu")

def save_model(model, path):
    torch.save(model.state_dict(), path)

def load_model():
    checkpoint = torch.load("neurogpt_trained_linear.pth", map_location=device)
    model.load_state_dict(checkpoint)
    model.eval()
    return model

def evaluate_model(model, test_loader):
    with torch.no_grad():
        predictions = []
        true_labels = []
        for datapoint, labels in test_loader:
            outputs = model(datapoint)
            _, predicted = torch.max(outputs.data, 1)
            predictions.append(predicted)
            true_labels.append(labels)
        predictions = torch.cat(predictions).numpy()
        true_labels = torch.cat(true_labels).numpy()
        accuracy = balanced_accuracy_score(true_labels, predictions)
        print(f"Test Accuracy: {accuracy:.4f}")
        return accuracy

# Getting the model (code adapted from GitHub Copilot)
from Model_NeuroGPT import EEGConformer
class NeuroGPTClassifier(torch.nn.Module):
    def __init__(self, input_channels, num_time_points, num_classes):
        super().__init__()

        self.channel_projection = torch.nn.Conv1d(in_channels=input_channels, out_channels=22, kernel_size=1)

        self.encoder = EEGConformer(n_outputs=num_classes, n_channels=22, n_times=num_time_points, is_decoding_mode=True)

        feature_dim = self.encoder.get_fc_size()
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

model = NeuroGPTClassifier(input_channels=eeg_data.data.shape[1], num_time_points=eeg_data.data.shape[2], num_classes=5)



optimizer = torch.optim.Adam([*model.channel_projection.parameters(), *model.classifier.parameters()], lr=0.003, weight_decay=0.1, eps=1e-8)
# Line below from OpenChamber (DeepSeek V4.1 Flash 1017)
loss_function = torch.nn.CrossEntropyLoss(weight=torch.tensor([1, 1, 0.2, 1, 1]))


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
        model.encoder.eval()  # Freeze the encoder during training
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

if __name__ == "__main__":
    print(f"Training ...")
    train_data_tensor, train_labels_tensor, test_data_tensor, test_labels_tensor = dataset
    train_data_tensor = train_data_tensor.to(device)
    train_labels_tensor = train_labels_tensor.to(device)
    test_data_tensor = test_data_tensor.to(device)
    test_labels_tensor = test_labels_tensor.to(device)

    train_dataset = TensorDataset(train_data_tensor, train_labels_tensor)
    test_dataset = TensorDataset(test_data_tensor, test_labels_tensor)

    train_loader = DataLoader(train_dataset, batch_size=32, shuffle=True)
    test_loader = DataLoader(test_dataset, batch_size=32, shuffle=False)

    model = NeuroGPTClassifier(input_channels=eeg_data.data.shape[1], num_time_points=eeg_data.data.shape[2], num_classes=5)
    model.to(device)

    train_model(model, epochs=67, data=train_loader, val_data=test_loader)
    save_model(model, "neurogpt_trained_linear.pth")
    load_model()
    acc = evaluate_model(model, test_loader)