# Colab installs
!pip install seaborn
!pip install torch-geometric
!pip install networkx
!pip install ucimlrepo
!pip install xgboost

# Core imports
import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
import networkx as nx
import matplotlib.pyplot as plt
import seaborn as sns

# Scikit-learn imports
from sklearn.preprocessing import StandardScaler, LabelEncoder
from sklearn.model_selection import train_test_split, KFold
from sklearn.metrics import classification_report, confusion_matrix, f1_score, accuracy_score
from sklearn.neighbors import kneighbors_graph
from sklearn.metrics import accuracy_score

# PyTorch Geometric imports
from torch_geometric.nn import GCNConv
from torch_geometric.utils import from_networkx

# XGBoost
import xgboost as xgb

# UCI ML repository import
from ucimlrepo import fetch_ucirepo

# Itertools
import itertools

class DataLoader():
  def __init__(self, parameters, dataset):
    self.parameters = parameters
    self.dataset = dataset
    self.loaded_dataset = fetch_ucirepo(id=dataset['id'])

class DataProcessor():
  def __init__(self, parameters, pipeline_registry, dataset_name):
    self.parameters = parameters
    self.pipeline_registry = pipeline_registry
    self.dataset_name = dataset_name
    self.device = parameters['device']
    self.loaded_dataset = pipeline_registry[dataset_name]['data_loader'].loaded_dataset
    self.X = self.loaded_dataset.data.features
    self.X_numerical_features, self.X_categorical_features = self.split_feature_types()

    if self.X_numerical_features.empty:
      self.X_numeric_scaled = pd.DataFrame()
    else:
      self.X_numeric_scaled = self.scale_numeric()

    if self.X_categorical_features.empty:
      self.X_categorical_encoded = pd.DataFrame()
    else:
      self.X_categorical_encoded = pd.get_dummies(self.X_categorical_features)

    self.X_prepared = pd.concat([self.X_numeric_scaled, self.X_categorical_encoded], axis=1)
    self.X_prepared = self.X_prepared.apply(pd.to_numeric, errors='coerce').fillna(0).astype(float)
    self.x_tensor = torch.tensor(self.X_prepared.values.astype(np.float32), dtype=torch.float).to(self.device)

    self.y = self.loaded_dataset.data.targets
    self.y_encoded = self.encode_target()
    self.num_classes = len(self.y_encoded['target'].unique())
    self.y_tensor = torch.tensor(self.y_encoded.values.ravel(), dtype=torch.long).to(self.device)

  def split_feature_types(self):
    numerical_features = self.X.select_dtypes(include=[np.number])
    categorical_features = self.X.select_dtypes(exclude=[np.number])
    return numerical_features, categorical_features

  def scale_numeric(self):
    scaler = StandardScaler()
    X_numeric_scaled = pd.DataFrame(scaler.fit_transform(self.X_numerical_features), columns=self.X_numerical_features.columns)
    return X_numeric_scaled

  def encode_target(self):
    encoder = LabelEncoder()
    y_encoded = pd.DataFrame(encoder.fit_transform(self.y.values.ravel()), columns=['target'])
    return y_encoded

class DataSplitter():
    def __init__(self, parameters):
        self.random_seed = parameters['random_seed']
        self.kfold = KFold(n_splits=10, shuffle=True, random_state=self.random_seed)

    def split(self, X, y):
        return self.kfold.split(X, y)

    def train_test_split(self, X, y, test_size=0.1):
        return train_test_split(X, y, test_size=test_size, random_state=self.random_seed)

class MLP(torch.nn.Module):
    def __init__(self, input_dim, hidden_dim, num_hidden_layers, output_dim):
        super(MLP, self).__init__()
        layers = []
        for i in range(num_hidden_layers):
            if i == 0:
                layers.append(torch.nn.Linear(input_dim, hidden_dim))
            else:
                layers.append(torch.nn.Linear(hidden_dim, hidden_dim))
            layers.append(torch.nn.ReLU())
        layers.append(torch.nn.Linear(hidden_dim, output_dim))
        self.network = torch.nn.Sequential(*layers)

    def forward(self, x):
        return self.network(x)

class MLPModel():
    def __init__(self, parameters, pipeline_registry, dataset_name):
        self.parameters = parameters
        self.pipeline_registry = pipeline_registry
        self.dataset_name = dataset_name
        self.device = parameters['device']
        self.X = pipeline_registry[dataset_name]['data_processor'].x_tensor
        self.y = pipeline_registry[dataset_name]['data_processor'].y_tensor
        self.num_classes = pipeline_registry[dataset_name]['data_processor'].num_classes

        self.data_splitter = pipeline_registry[dataset_name]['data_splitter']

        self.results = {
            'f1_scores': [],
            'accuracy_scores': [],
            'best_hyperparameters': []
        }

        self.run_model()  # Start model training/validation pipeline

    def run_model(self):
        kfold = self.data_splitter.kfold
        final_f1_scores = []
        final_accuracy_scores = []
        final_hyperparameters = []

        # Iterate over 10 folds
        for fold_idx, (train_val_idx, test_idx) in enumerate(kfold.split(self.X.cpu(), self.y.cpu())):
            print(f"\nRunning fold {fold_idx + 1}/10...")

            # Prepare train, validation, and test data for this fold
            X_train_val, X_test = self.X[train_val_idx], self.X[test_idx]
            y_train_val, y_test = self.y[train_val_idx], self.y[test_idx]

            best_model = None
            best_score = -float('inf')
            best_params = None
            param_grid = self.get_param_grid()

            # Hyperparameter search for each fold
            for params in param_grid:
                model = MLP(
                    input_dim=self.X.shape[1],
                    hidden_dim=params['hidden_dim'],
                    num_hidden_layers=params['num_hidden_layers'],
                    output_dim=self.num_classes
                ).to(self.device)

                optimizer = torch.optim.Adam(
                    model.parameters(), lr=params['lr'], weight_decay=params['weight_decay']
                )
                criterion = torch.nn.CrossEntropyLoss()

                # Train and validate on train_val set
                self.train_model(model, (X_train_val, y_train_val), params['epochs'], optimizer, criterion)

                val_preds = self.predict(model, X_train_val)
                val_score = f1_score(y_train_val.cpu(), val_preds.cpu(), average='weighted')

                # Track the best model and hyperparameters for this fold
                if val_score > best_score:
                    best_score = val_score
                    best_model = model
                    best_params = params

            print(f"Best validation F1 score for fold {fold_idx + 1}: {best_score:.4f}")
            print(f"Best hyperparameters for fold {fold_idx + 1}: {best_params}")

            # Retrain using best model and hyperparameters and evaluate on test set
            fold_f1_scores = []
            fold_accuracy_scores = []
            for retrain_run in range(3):  # Repeat 3 times for average metrics
                print(f"Retraining best model (run {retrain_run + 1}/3)...")
                # Ensure optimizer is re-initialized for retraining
                optimizer = torch.optim.Adam(
                    best_model.parameters(), lr=best_params['lr'], weight_decay=best_params['weight_decay']
                )
                self.train_model(best_model, (X_train_val, y_train_val),
                                 best_params['epochs'], optimizer, criterion)

                test_preds = self.predict(best_model, X_test)
                test_f1 = f1_score(y_test.cpu(), test_preds.cpu(), average='weighted')
                test_accuracy = accuracy_score(y_test.cpu(), test_preds.cpu())
                fold_f1_scores.append(test_f1)
                fold_accuracy_scores.append(test_accuracy)

            # Store metrics for this fold
            avg_f1 = np.mean(fold_f1_scores)
            avg_accuracy = np.mean(fold_accuracy_scores)
            print(f"Fold {fold_idx + 1} - Avg F1: {avg_f1:.4f}, Avg Accuracy: {avg_accuracy:.4f}")

            final_f1_scores.append(avg_f1)
            final_accuracy_scores.append(avg_accuracy)
            final_hyperparameters.append(best_params)

        # Store results
        self.results['f1_scores'] = final_f1_scores
        self.results['accuracy_scores'] = final_accuracy_scores
        self.results['best_hyperparameters'] = final_hyperparameters

        # Display final results
        print("\nFinal Results:")
        print(f"F1 Score - Mean: {np.mean(final_f1_scores):.4f}, Std: {np.std(final_f1_scores):.4f}")
        print(f"Accuracy - Mean: {np.mean(final_accuracy_scores):.4f}, Std: {np.std(final_accuracy_scores):.4f}")
        print("\nBest Hyperparameters for Each Fold:")
        for i, params in enumerate(final_hyperparameters, start=1):
            print(f"Fold {i}: {params}")

    # Helper functions for training and prediction remain unchanged

        most_common_params = max(set(tuple(d.items()) for d in final_hyperparameters),
                                 key=lambda x: final_hyperparameters.count(dict(x)))
        print(f"\nMost Frequently Selected Hyperparameters: {dict(most_common_params)}")

    def train_model(self, model, train_data, epochs, optimizer, criterion):
        model.train()
        X_train, y_train = train_data
        for epoch in range(epochs):
            optimizer.zero_grad()
            output = model(X_train)
            loss = criterion(output, y_train)
            loss.backward()
            optimizer.step()

    def predict(self, model, X):
        model.eval()
        with torch.no_grad():
            logits = model(X)
            predictions = torch.argmax(logits, dim=1)
        return predictions

    def get_param_grid(self):
        lr_grid = self.parameters['mlp_model']['lr_grid']
        hidden_dims = self.parameters['mlp_model']['hidden_dim']
        num_hidden_layers = self.parameters['mlp_model']['num_hidden_layers']
        weight_decay_grid = self.parameters['mlp_model']['weight_decay']
        epochs = self.parameters['mlp_model']['epochs']

        param_grid = []
        for lr, dim, layers, wd in itertools.product(lr_grid, hidden_dims, num_hidden_layers, weight_decay_grid):
            param_grid.append({
                'lr': lr,
                'hidden_dim': dim,
                'num_hidden_layers': layers,
                'weight_decay': wd,
                'epochs': epochs
            })
        return param_grid

#Main
def build_parameters():
  device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
  random_seed = 42

  datasets = [
              {'name': 'abalone',
               'id': 1,},
              {'name': 'adult',
               'id': 2,},
              {'name': 'dry_bean',
               'id': 602,},
              {'name': 'isolet',
               'id': 54,},
              {'name': 'musk_v2',
               'id': 75,},
              ]

  mlp_model = {
      'hidden_dim': [32, 64, 128],
      'num_hidden_layers': [1, 2, 3],
      'epochs': 1000,
      'lr_grid': [0.01, 0.001],
      'weight_decay': [0, 5e-4],
  }

  return {
          'device': device,
          'random_seed': random_seed,
          'datasets': datasets,
          'mlp_model': mlp_model,
          }

def build_pipeline_registry(dataset_names):
  pipeline_registry = {}
  for _, dataset_name in enumerate(dataset_names):
    pipeline_registry.setdefault(dataset_name, {})
  return pipeline_registry

def main():
    parameters = build_parameters()
    torch.manual_seed(parameters['random_seed'])
    np.random.seed(parameters['random_seed'])
    dataset_names = [dataset['name'] for dataset in parameters['datasets']]
    pipeline_registry = build_pipeline_registry(dataset_names)

    for dataset in parameters['datasets']:
        dataset_name = dataset['name']
        print("--------------------------------")
        print(f"Loading dataset: {dataset_name}")
        print("--------------------------------")
        pipeline_registry[dataset_name]['data_loader'] = DataLoader(parameters=parameters, dataset=dataset)
        pipeline_registry[dataset_name]['data_processor'] = DataProcessor(parameters=parameters, pipeline_registry=pipeline_registry, dataset_name=dataset_name)
        pipeline_registry[dataset_name]['data_splitter'] = DataSplitter(parameters=parameters)
        pipeline_registry[dataset_name]['mlp_model'] = MLPModel(parameters=parameters, pipeline_registry=pipeline_registry, dataset_name=dataset_name)

main()