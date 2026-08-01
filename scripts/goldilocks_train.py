#!/usr/bin/env python3
"""
goldilocks_train.py — Train dual LightGBM classifiers for Goldilocks prediction.

Trains two separate LightGBM classifiers:
  - M_high: Predicts P(Goldilocks HIGH event | features)
  - M_low:  Predicts P(Goldilocks LOW event | features)

Key design choices:
  - Temporal train/test split (not random — respects time series ordering)
  - Class imbalance handling via scale_pos_weight
  - Feature importance analysis
  - AUC-ROC, precision-recall, calibration curve
  - No scikit-learn dependency (pure LightGBM + numpy)

Outputs:
  - data/models/goldilocks_high_model.txt      (LightGBM booster)
  - data/models/goldilocks_low_model.txt       (LightGBM booster)
  - data/models/goldilocks_feature_importance.csv
  - data/models/goldilocks_training_report.json

Usage:
    python3 scripts/goldilocks_train.py
    python3 scripts/goldilocks_train.py --features data/goldilocks_features_KNYC.csv --labels data/goldilocks_labels_KNYC.csv

B-Mode compliant. LightGBM only. Deterministic with fixed seed.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, 'data')
MODEL_DIR = os.path.join(DATA_DIR, 'models')

# LightGBM may need LD_LIBRARY_PATH for libgomp — warn if import fails
try:
    import lightgbm as lgb
except ImportError as e:
    print(f"ERROR: LightGBM not available: {e}")
    print("Try: export LD_LIBRARY_PATH=/home/node/.local/lib:$LD_LIBRARY_PATH")
    sys.exit(1)

# Default feature columns (numeric only — LightGBM handles categoricals as int)
FEATURE_COLS_NUMERIC = [
    'wind_avg_kt', 'wind_max_kt', 'wind_stddev_3hr',
    'wind_3pm_kt', 'wind_sunset_kt', 'wind_6am_kt',
    'dp_depression_C', 'cloud_cover_frac', 'cloud_ceiling_ft',
    'solar_elevation_max', 'solar_flux_est', 'longwave_flux_est',
    'daily_temp_range_C', 'lapse_rate_850_925',
    'bulk_richardson', 'inversion_strength_proxy',
    'day_of_year', 'is_weekend', 'month',
    'daylight_hours', 'nwp_cloud_cover', 'nwp_wind_speed_kt',
    'temp_range_forecast', 'goldilocks_prev_day',
    'goldilocks_prev_3days', 'goldilocks_rate_30d',
]

# Categorical features (encoded as int for LightGBM)
FEATURE_COLS_CATEGORICAL = [
    'synoptic_class',
    'wind_direction_sector',
]

SEED = 42


def _prepare_data(df, feature_cols_numeric=None, feature_cols_categorical=None):
    """
    Prepare feature matrix and handle missing values + categorical encoding.

    LightGBM handles NaN natively. Categorical features are encoded as ints.
    """
    import pandas as pd

    if feature_cols_numeric is None:
        feature_cols_numeric = FEATURE_COLS_NUMERIC
    if feature_cols_categorical is None:
        feature_cols_categorical = FEATURE_COLS_CATEGORICAL

    df = df.copy()

    # Encode categorical features
    cat_encoders = {}
    for col in feature_cols_categorical:
        if col in df.columns:
            df[col] = df[col].astype('category').cat.codes
            df[col] = df[col].replace(-1, np.nan).astype('float')
            cat_encoders[col] = {
                'categories': df[col].dropna().unique().tolist()
            }

    all_features = feature_cols_numeric + \
        [c for c in feature_cols_categorical if c in df.columns]

    # Ensure all feature columns exist
    missing = [c for c in all_features if c not in df.columns]
    if missing:
        print(f"WARNING: Missing feature columns: {missing}")

    X = df[[c for c in all_features if c in df.columns]]
    return X, cat_encoders


def _temporal_split(df, test_size=0.2):
    """
    Temporal (time-ordered) train/test split.
    Uses last test_size fraction of dates as test set.
    """
    df = df.sort_values('local_date').reset_index(drop=True)
    n = len(df)
    split_idx = int(n * (1 - test_size))
    train_df = df.iloc[:split_idx].copy()
    test_df = df.iloc[split_idx:].copy()
    print(f"Train dates: {train_df['local_date'].iloc[0]} to {train_df['local_date'].iloc[-1]}")
    print(f"Test dates:  {test_df['local_date'].iloc[0]} to {test_df['local_date'].iloc[-1]}")
    return train_df, test_df


def _compute_baselines(y):
    """Compute baseline metrics for comparison."""
    base_rate = y.mean()
    brier_clim = base_rate * (1 - base_rate) ** 2 + (1 - base_rate) * base_rate ** 2
    return {
        'base_rate': float(round(base_rate, 4)),
        'brier_climatology': float(round(brier_clim, 6)),
    }


def train_goldilocks_model(
    features_path: str,
    labels_path: str,
    target_col: str = 'is_goldilocks_any',
    output_prefix: str = 'goldilocks',
    test_size: float = 0.2,
    hyperparams: Optional[dict] = None,
) -> dict:
    """
    Train a single LightGBM classifier for the given target.

    Args:
        features_path: Path to feature matrix CSV
        labels_path: Path to labels CSV
        target_col: Target column name
        output_prefix: Prefix for model files
        test_size: Fraction of data for temporal test set
        hyperparams: Optional LightGBM hyperparameter overrides

    Returns:
        Dict with training results
    """
    import pandas as pd

    print(f"\n{'='*60}")
    print(f"Training {output_prefix} model (target: {target_col})")
    print(f"{'='*60}")

    # Load data
    df = pd.read_csv(features_path)

    # Only merge labels if target column is not already in features
    if target_col not in df.columns:
        labels = pd.read_csv(labels_path)
        df = df.merge(labels[['date', target_col]], left_on='local_date',
                      right_on='date', how='inner')

    n_total = len(df)
    n_events = int(df[target_col].sum())
    print(f"Total samples: {n_total}")
    print(f"Events ({target_col}): {n_events} ({n_events/max(n_total,1)*100:.2f}%)")

    if n_events < 5:
        print(f"WARNING: Too few events ({n_events}) for meaningful training")
        return {'status': 'skipped', 'reason': f'too_few_events: {n_events}'}

    # Baseline
    baselines = _compute_baselines(df[target_col].values)
    print(f"Baseline rate: {baselines['base_rate']:.4f}")
    print(f"Brier (climatology): {baselines['brier_climatology']:.6f}")

    # Temporal split
    train_df, test_df = _temporal_split(df, test_size)

    X_train, cat_enc = _prepare_data(train_df)
    y_train = train_df[target_col].values
    X_test, _ = _prepare_data(test_df)
    y_test = test_df[target_col].values

    print(f"Train: {len(X_train)} samples, {int(y_train.sum())} events ({y_train.mean()*100:.2f}%)")
    print(f"Test:  {len(X_test)} samples, {int(y_test.sum())} events ({y_test.mean()*100:.2f}%)")
    print(f"Features: {list(X_train.columns)}")

    # Handle class imbalance
    neg_count = int((y_train == 0).sum())
    pos_count = int(y_train.sum())
    scale_pos_weight = neg_count / max(pos_count, 1)

    # Default hyperparams
    params = {
        'objective': 'binary',
        'metric': ['binary_logloss', 'auc', 'average_precision'],
        'boosting_type': 'gbdt',
        'num_leaves': 31,
        'learning_rate': 0.05,
        'feature_fraction': 0.8,
        'bagging_fraction': 0.8,
        'bagging_freq': 5,
        'verbose': -1,
        'seed': SEED,
        'scale_pos_weight': scale_pos_weight,
        'min_data_in_leaf': 20,
        'num_threads': 2,
    }
    if hyperparams:
        params.update(hyperparams)

    # Identify categorical features
    cat_feature_list = [
        i for i, col in enumerate(X_train.columns)
        if col in FEATURE_COLS_CATEGORICAL
    ]

    # Create LightGBM datasets
    train_data = lgb.Dataset(
        X_train, label=y_train,
        categorical_feature=cat_feature_list if cat_feature_list else None,
        free_raw_data=False,
    )
    test_data = lgb.Dataset(
        X_test, label=y_test,
        categorical_feature=cat_feature_list if cat_feature_list else None,
        reference=train_data,
        free_raw_data=False,
    )

    # Train
    print(f"\nTraining LightGBM with {params}...")
    start = time.time()

    model = lgb.train(
        params,
        train_data,
        valid_sets=[train_data, test_data],
        valid_names=['train', 'test'],
        num_boost_round=500,
        callbacks=[
            lgb.early_stopping(50, verbose=True),
            lgb.log_evaluation(100),
        ],
    )

    elapsed = time.time() - start
    best_iter = model.best_iteration
    print(f"Training completed in {elapsed:.1f}s ({best_iter} iterations)")

    # Evaluate
    y_pred_train = model.predict(X_train)
    y_pred_test = model.predict(X_test)

    # Metrics (pure numpy, no sklearn)
    metrics = _compute_metrics(y_train, y_pred_train, 'train')
    test_metrics = _compute_metrics(y_test, y_pred_test, 'test')
    metrics.update({f'test_{k}': v for k, v in test_metrics.items()})

    # Feature importance
    importance_df = _feature_importance(model, list(X_train.columns))

    # Save model
    os.makedirs(MODEL_DIR, exist_ok=True)
    model_path = os.path.join(MODEL_DIR, f'{output_prefix}_{target_col}_model.txt')
    model.save_model(model_path)
    print(f"Model saved to {model_path}")

    # Save feature importance
    imp_path = os.path.join(MODEL_DIR, f'{output_prefix}_{target_col}_feature_importance.csv')
    importance_df.to_csv(imp_path, index=False)
    print(f"Feature importance saved to {imp_path}")

    # Compile report
    report = {
        'status': 'trained',
        'model_path': model_path,
        'feature_importance_path': imp_path,
        'target': target_col,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'n_train': len(X_train),
        'n_test': len(X_test),
        'n_events_train': int(y_train.sum()),
        'n_events_test': int(y_test.sum()),
        'base_rate': baselines['base_rate'],
        'brier_climatology': baselines['brier_climatology'],
        'best_iteration': best_iter,
        'training_time_s': round(elapsed, 1),
        'hyperparams': params,
        'metrics': metrics,
        'cat_encoders': {k: v for k, v in cat_enc.items()},
        'feature_cols_numeric': FEATURE_COLS_NUMERIC,
        'feature_cols_categorical': FEATURE_COLS_CATEGORICAL,
        'test_date_range': [
            str(test_df['local_date'].iloc[0]),
            str(test_df['local_date'].iloc[-1]),
        ],
    }

    return report


def _compute_metrics(y_true, y_pred, label=''):
    """Compute classification metrics using pure numpy."""
    y_true = np.array(y_true)
    y_pred = np.array(y_pred)

    # Brier score
    brier = float(np.mean((y_pred - y_true) ** 2))

    # Log loss (avoid log(0))
    eps = 1e-15
    y_pred_clip = np.clip(y_pred, eps, 1 - eps)
    logloss = float(-np.mean(
        y_true * np.log(y_pred_clip) + (1 - y_true) * np.log(1 - y_pred_clip)
    ))

    # AUC-ROC (via rank-based method for small datasets, fallback for all)
    pos_scores = y_pred[y_true == 1]
    neg_scores = y_pred[y_true == 0]
    if len(pos_scores) > 0 and len(neg_scores) > 0:
        n_pos = len(pos_scores)
        n_neg = len(neg_scores)
        # Count pairs where pos_score > neg_score
        auc_roc = 0.0
        for ps in pos_scores:
            auc_roc += (ps > neg_scores).sum()
        auc_roc = float(auc_roc / (n_pos * n_neg))
    else:
        auc_roc = 0.5

    # Average Precision (AUC-PR)
    sorted_idx = np.argsort(y_pred)[::-1]
    y_true_sorted = y_true[sorted_idx]
    cum_prec = 0.0
    n_pos_total = y_true.sum()
    if n_pos_total > 0:
        tp = 0
        for i, true_val in enumerate(y_true_sorted):
            if true_val == 1:
                tp += 1
                cum_prec += tp / (i + 1)
        auc_pr = float(cum_prec / n_pos_total)
    else:
        auc_pr = 0.0

    # Precision@various thresholds
    precisions = {}
    for thresh in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]:
        hits = y_pred >= thresh
        if hits.sum() > 0:
            prec = float((y_true[hits] == 1).mean())
        else:
            prec = 0.0
        precisions[f'precision@{thresh:.0%}'] = round(prec, 4)
        precisions[f'recall@{thresh:.0%}'] = round(
            float((y_true[hits] == 1).sum() / max(n_pos_total, 1)), 4)

    # Calibration: grouped into 10 bins
    bin_edges = np.linspace(0, 1, 11)
    bin_indices = np.digitize(y_pred, bin_edges) - 1
    cal_curve = []
    for b in range(10):
        mask = bin_indices == b
        if mask.sum() > 0:
            mean_pred = float(y_pred[mask].mean())
            mean_obs = float(y_true[mask].mean())
            cal_curve.append({
                'bin': b,
                'n': int(mask.sum()),
                'mean_predicted': round(mean_pred, 3),
                'mean_observed': round(mean_obs, 3),
            })

    # Reliability slope (linear fit of observed vs predicted)
    cal_arr = np.array([(c['mean_predicted'], c['mean_observed'])
                        for c in cal_curve if c['n'] >= 5])
    if len(cal_arr) >= 3:
        slope = float(np.polyfit(cal_arr[:, 0], cal_arr[:, 1], 1)[0])
    else:
        slope = None

    metrics = {
        f'brier_{label}': round(brier, 6),
        f'logloss_{label}': round(logloss, 4),
        f'auc_roc_{label}': round(auc_roc, 4),
        f'auc_pr_{label}': round(auc_pr, 4),
        f'calibration_slope_{label}': round(slope, 3) if slope is not None else None,
        f'calibration_curve_{label}': cal_curve,
        f'precision_metrics_{label}': precisions,
    }

    return metrics


def _feature_importance(model, feature_names):
    """Extract feature importance from LightGBM model."""
    import pandas as pd
    gain_imp = model.feature_importance(importance_type='gain')
    split_imp = model.feature_importance(importance_type='split')

    imp_df = pd.DataFrame({
        'feature': feature_names,
        'gain_importance': gain_imp,
        'split_importance': split_imp,
        'gain_pct': gain_imp / max(gain_imp.sum(), 1) * 100,
        'split_pct': split_imp / max(split_imp.sum(), 1) * 100,
    })
    imp_df = imp_df.sort_values('gain_importance', ascending=False).reset_index(drop=True)
    return imp_df


def main():
    parser = argparse.ArgumentParser(
        description='Train Goldilocks predictive models (dual LightGBM)')
    parser.add_argument('--features', default=None,
                        help='Feature matrix CSV path')
    parser.add_argument('--labels', default=None,
                        help='Labels CSV path')
    parser.add_argument('--output', default=None,
                        help='Training report output path')
    parser.add_argument('--test-size', type=float, default=0.2,
                        help='Fraction for temporal test set (default: 0.2)')
    parser.add_argument('--num-boost-round', type=int, default=500,
                        help='Max boosting rounds (default: 500)')
    parser.add_argument('--learning-rate', type=float, default=0.05,
                        help='Learning rate (default: 0.05)')
    parser.add_argument('--num-leaves', type=int, default=31,
                        help='Tree max leaves (default: 31)')
    parser.add_argument('--skip-high', action='store_true',
                        help='Skip training HIGH model')
    parser.add_argument('--skip-low', action='store_true',
                        help='Skip training LOW model')
    args = parser.parse_args()

    # Resolve paths
    if args.features:
        features_path = args.features
    else:
        features_path = os.path.join(
            DATA_DIR, f'goldilocks_features_KNYC.csv')
    if args.labels:
        labels_path = args.labels
    else:
        labels_path = os.path.join(
            DATA_DIR, f'goldilocks_labels_KNYC.csv')

    if not os.path.exists(features_path):
        print(f"ERROR: Features not found: {features_path}")
        print("Run goldilocks_feature_engineering.py first")
        sys.exit(1)
    if not os.path.exists(labels_path):
        print(f"ERROR: Labels not found: {labels_path}")
        print("Run goldilocks_labeling.py first")
        sys.exit(1)

    hyperparams = {
        'learning_rate': args.learning_rate,
        'num_leaves': args.num_leaves,
    }

    reports = {}
    import pandas as pd

    df_feat = pd.read_csv(features_path)

    # Check if labels are already in features (from goldilocks_feature_engineering)
    if 'is_goldilocks_any' not in df_feat.columns:
        df_labels = pd.read_csv(labels_path)
        df_feat = df_feat.merge(
            df_labels[['date', 'is_goldilocks_any', 'is_goldilocks_high', 'is_goldilocks_low']],
            left_on='local_date', right_on='date', how='left'
        )
        df_feat = df_feat.drop(columns=['date'])

    # Ensure goldilocks history features exist (compute if missing)
    df_feat = df_feat.sort_values('local_date').reset_index(drop=True)
    if 'goldilocks_prev_day' not in df_feat.columns:
        df_feat['goldilocks_prev_day'] = (
            df_feat['is_goldilocks_any'].shift(1).fillna(0).astype(int)
        )
    if 'goldilocks_prev_3days' not in df_feat.columns:
        df_feat['goldilocks_prev_3days'] = (
            df_feat['is_goldilocks_any'].rolling(3, min_periods=1).sum().shift(1).fillna(0)
        ).astype(int)
    if 'goldilocks_rate_30d' not in df_feat.columns:
        df_feat['goldilocks_rate_30d'] = (
            df_feat['is_goldilocks_any'].rolling(30, min_periods=1).mean().shift(1).fillna(0.0)
        )

    # Save enriched features only if labels weren't already there
    enriched_path = features_path.replace('.csv', '_enriched.csv')
    df_feat.to_csv(enriched_path, index=False)
    print(f"Enriched features saved to {enriched_path}")

    # Train HIGH model
    if not args.skip_high:
        print("\n" + "="*60)
        print("TRAINING GOLDILOCKS HIGH MODEL")
        print("="*60)
        high_report = train_goldilocks_model(
            enriched_path, labels_path,
            target_col='is_goldilocks_high',
            output_prefix='goldilocks',
            test_size=args.test_size,
            hyperparams=hyperparams,
        )
        reports['high'] = high_report
    else:
        reports['high'] = {'status': 'skipped'}

    # Train LOW model
    if not args.skip_low:
        print("\n" + "="*60)
        print("TRAINING GOLDILOCKS LOW MODEL")
        print("="*60)
        low_report = train_goldilocks_model(
            enriched_path, labels_path,
            target_col='is_goldilocks_low',
            output_prefix='goldilocks',
            test_size=args.test_size,
            hyperparams=hyperparams,
        )
        reports['low'] = low_report
    else:
        reports['low'] = {'status': 'skipped'}

    # Save training report
    output_path = args.output or os.path.join(
        MODEL_DIR, 'goldilocks_training_report.json')
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)

    full_report = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'features_path': features_path,
        'labels_path': labels_path,
        'models': reports,
    }
    with open(output_path, 'w') as f:
        json.dump(full_report, f, indent=2)
    print(f"\nTraining report saved to {output_path}")
    print("Done.")

    return 0


if __name__ == '__main__':
    sys.exit(main())