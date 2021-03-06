import os
import numpy as np
import pandas as pd
from multiprocessing import Pool

from sklearn.calibration import CalibratedClassifierCV

# Classifier for comparison
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import Perceptron

# Generation Pool techniques
from sklearn.ensemble import BaggingClassifier
from sklearn.ensemble import AdaBoostClassifier
from sgh import SGH

# Selection phase (DCS)
from deslib.dcs.ola import OLA
from deslib.dcs.lca import LCA
from deslib.dcs.rank import Rank
from deslib.dcs.mcb import MCB

# Selection phase (DES)
from deslib.des.knora_u import KNORAU
from deslib.des.knora_e import KNORAE
from deslib.des.des_mi import DESMI
from deslib.des.des_knn import DESKNN
from deslib.des.des_clustering import DESClustering
from deslib.des.des_p import DESP
from deslib.des.meta_des import METADES
from deslib.des.knop import KNOP

# Imbalancing Learning
from imblearn.over_sampling import SMOTE
from imblearn.over_sampling import RandomOverSampler
from imblearn.under_sampling import InstanceHardnessThreshold
from imblearn.under_sampling import RandomUnderSampler

# Baseline Methods
from deslib.static.oracle import Oracle

# Common functions
from utils import gen_ensemble, ds_ensemble, balance_dataset
from utils import load_dataset, build_results_df, save_results_df

# Metrics
from metrics import *

# Plot Confusion Matrix
from graphs_plotter import plot_confusion_matrix

# Number of trees for each fold in ['HH103', 'HH124', 'HH129', 'Kyoto2008','Kyoto2009Spring']
params = [[70, 80, 80, 80, 50, 90], [60, 60, 30, 40, 60, 50], [
    90, 80, 80, 90, 80, 90], [50, 60, 20, 20, 20, 60], [100, 80, 90, 90, 90, 80]]


def process_generation(args):
    X_train = args['X_train']
    y_train = args['y_train']
    gen_method = args['gen_method']
    imb_method = args['imb_method']

    if imb_method:
        # In cases where the imbalanced learning techniques have been used
        X_train, y_train = balance_dataset(X_train, y_train, imb_method)

    # Generation method
    base = Perceptron(max_iter=1000, n_jobs=-1)
    n_estimators = 100

    return gen_ensemble(X_train, y_train, gen_method, base, n_estimators)


def process_metrics(y_test, predictions):
    mfm = multi_label_Fmeasure(y_test, predictions)
    gmean = geometric_mean(y_test, predictions, "multiclass")
    acc_by_class = accuracy_by_class(y_test, predictions)
    acc = accuracy(y_test, predictions)
    prec = precision(y_test, predictions, "macro")
    rec = recall(y_test, predictions, "macro")
    fmeasure = fmeasure_score(y_test, predictions, "macro")

    return [mfm, gmean, acc, prec, rec, fmeasure, acc_by_class]


def process_selection(args):
    X_train = args['X_train']
    y_train = args['y_train']
    X_test = args['X_test']
    y_test = args['y_test']
    pool_clf = args['pool_clf']
    fold_name = args['fold_name']
    gen_method = args['gen_method']
    method = args['ds_method']
    params_rf = args['params']

    # Evaluation considering Random Forest
    if method == RandomForestClassifier:
        ensemble = RandomForestClassifier(n_estimators=params_rf)
        ensemble.fit(X_train, y_train)
        predictions = ensemble.predict(X_test)

    else:
        # Since the SGH was not built to predict probabilities,
        # the perceptrons should be previously trained
        if gen_method == SGH and (method == METADES or method == KNOP):
            calibrated_pool = []
            for clf in pool_clf:
                calibrated = CalibratedClassifierCV(base_estimator=clf, cv='prefit')
                calibrated.fit(X_train, y_train)
                calibrated_pool.append(calibrated)

            pool_clf = calibrated_pool

        # Dynamic Selection techniques
        ensemble = ds_ensemble(X_train, y_train, pool_clf, method)

    # In prediction phase, two options: Oracle and DS techniques
    if method == Oracle:
        predictions = ensemble.predict(X_test, y_test)
    else:
        predictions = ensemble.predict(X_test)

    conf_matrix = confusion_matrix_score(y_test, predictions)
    metrics = process_metrics(y_test, predictions)

    return [fold_name, conf_matrix, metrics, predictions]


def experiment_parameters(folds, noise, labels_dict):
    args_list = []

    for f, fold in enumerate(folds):
        args = {'X_train': np.array(fold.xTrain)}

        # Brew requires numeric class labels
        y_train = np.array(fold.yTrains[noise])
        args['y_train'] = np.array([labels_dict.get(x) for x in y_train])
        # X test
        args['X_test'] = np.array(fold.xTest)

        # Brew requires numeric class labels
        y_test = np.array(fold.yTest)
        args['y_test'] = np.array([labels_dict.get(x) for x in y_test])
        args_list.append(args)

    return args_list


def experiment_generation(parameters, gen_method, imb_method):
    generation = []

    for param in parameters:
        param['gen_method'] = gen_method
        param['imb_method'] = imb_method
        generation.append(param)

    return list(map(process_generation, generation))


def experiment_selection(parameters, pool_gen, iteration, gen_method, dyn_selector, noise):
    pool = Pool(5)
    jobs = []

    for f, (param, pool_clf) in enumerate(zip(parameters, pool_gen)):
        param['pool_clf'] = pool_clf
        param['fold_name'] = 'Fold_' + str(f + 1)
        param['noise'] = noise
        param['ds_method'] = dyn_selector
        param['gen_method'] = gen_method
        param['params'] = params[iteration][f]
        jobs.append(param)

    results = list(map(process_selection, jobs))

    pool.close()

    return results


def save_metrics(dataset, results, activities_list, labels_dict, gen_method, dyn_selector, imb_method):
    gen_method_name = str(gen_method).split('.')[-1].split('\'')[0]
    dyn_method_name = str(dyn_selector).split('.')[-1].split('\'')[0]
    imb_method_name = str(imb_method).split('.')[-1].split('\'')[0]

    folds_name = [result.pop(0) for result in results]
    # Get predictions
    predictions = np.concatenate([result.pop(-1) for result in results], axis=0)
    confusion_mx = [result.pop(0) for result in results]

    # Due to the different amount of classifiers for fold
    dfs = []
    for cm in confusion_mx:
        data = pd.DataFrame(0, index=np.arange(len(activities_list)), columns=activities_list)
        for i, row in enumerate(cm):
            columnList = activities_list[:len(row)]
            data.loc[i, columnList] = row

        dfs.append(data)

    # Concat confusion matrix per folds
    comp_df = pd.concat(dfs, axis=0).groupby(level=0).sum()
    cm = comp_df.values

    plot_confusion_matrix(
        cm=cm,
        classes=labels_dict,
        normalize=True,
        title=dataset + ": " + dyn_method_name + " - " + str(noise) + "0%",
        dataset=dataset,
        gen_method=gen_method_name,
        dynamic_method=dyn_method_name,
        noise_level=str(noise)
    )

    metrics = ['MultiLabel-Fmeasure', 'Gmean', 'Accuracy', 'Precision', 'Recall', 'F1']

    accuracy_class_df, results_df = build_results_df(metrics, folds_name, results, activities_list)
    save_results_df(results_df, dataset, imb_method_name, gen_method_name, noise, dyn_method_name)
    save_results_df(accuracy_class_df, dataset, imb_method_name, gen_method_name, noise, dyn_method_name + "_by_class")


if __name__ == '__main__':
    root = os.path.dirname(__file__)

    baseline = [RandomForestClassifier]

    # Prototype Selection Methods
    imb_methods = [SMOTE, RandomOverSampler, RandomUnderSampler, InstanceHardnessThreshold]
    # Generation Methods
    gen_methods = [BaggingClassifier, AdaBoostClassifier, SGH]
    # Dynamic Selection Techniques

    ds_methods_dcs = [OLA, LCA, MCB, Rank]
    ds_methods_des = [KNORAU, KNORAE, DESKNN, DESP, DESMI, DESClustering, METADES, KNOP]
    ds_methods = baseline + ds_methods_dcs + ds_methods_des + [Oracle]

    datasets = ['HH103', 'HH124', 'HH129', 'Kyoto2008', 'Kyoto2009Spring']

    for iteration, dataset in enumerate(datasets):
        print('\n\n~~ Database : ' + dataset + ' ~~')
        folds_list, activities, examples_by_class = load_dataset(dataset)

        for noise in range(0, 6):
            print('== Noise Parameter --> ' + str(noise) + '0% ==\n')

            parameters = experiment_parameters(folds_list, noise, examples_by_class)
            for gen_method in gen_methods:
                print('** Gen Method: %s' % (str(gen_method).split('.')[-1].split('\'')[0]))
                # pool of classifiers
                pool_clf = experiment_generation(parameters, gen_method, None)

                for ds_method in ds_methods:
                    print('** DS Method: %s' % (str(ds_method).split('.')[-1].split('\'')[0] + ' **\n'))
                    results = experiment_selection(parameters, pool_clf, iteration, gen_method, ds_method, noise)
                    save_metrics(dataset, results, activities, examples_by_class, gen_method, ds_method, None)
