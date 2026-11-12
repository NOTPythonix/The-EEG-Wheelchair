from collect_data import run_for_seconds, label_to_class, filt_bufs, setup_board
from train import load_model
import torch

class_to_label = {v: k for k, v in label_to_class.items()}

def get_data():
    run_for_seconds(4.0)
    real_filt_bufs = filt_bufs["8-30"]
    # Grab the latest 4 seconds of data
    real_filt_bufs = real_filt_bufs[:, -500:]
    # convert to tensor for model
    data_tensor = torch.tensor(real_filt_bufs, dtype=torch.float32).unsqueeze(0)  # unsqueeze adds batch dimension
    return data_tensor

def get_result(data):
    model.eval()
    with torch.no_grad():
        outputs = model(data)
        probs = torch.softmax(outputs, dim=1)
        return probs

def convert_class_to_label(label):
    return class_to_label[label]

if __name__ == "__main__":
    model = load_model()
    setup_board()
    while True:
        print("Collecting data for 4 seconds")
        data = get_data()
        results = get_result(data)
        probability_per_class = {}
        for class_id, probability in enumerate(results[0]):
            class_label = convert_class_to_label(class_id)
            probability_per_class[class_label] = probability.item()
        print("Live inference (probability per class):")
        for class_label, probability in probability_per_class.items():
            print(f"{class_label}: {probability:.4f}")