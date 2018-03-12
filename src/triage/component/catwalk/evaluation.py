import logging
import time

import numpy
from sqlalchemy.orm import sessionmaker

from triage.component.results_schema import TestEvaluation, TrainEvaluation


from . import metrics
from .utils import db_retry, sort_predictions_and_labels


def generate_binary_at_x(test_predictions, x_value, unit='top_n'):
    """Generate subset of predictions based on top% or absolute

    Args:
        test_predictions (list) A list of predictions, sorted by risk desc
        x_value (int) The percentile or absolute value desired
        unit (string, default 'top_n') The subsetting method desired,
            either percentile or top_n

    Returns: (list) The predictions subset
    """
    if unit == 'percentile':
        cutoff_index = int(len(test_predictions) * (x_value / 100.00))
    else:
        cutoff_index = x_value
    test_predictions_binary = [
        1 if x < cutoff_index else 0
        for x in range(len(test_predictions))
    ]
    return test_predictions_binary


class ModelEvaluator(object):
    """An object that can score models based on its known metrics"""

    """Available metric calculation functions

    Each value is expected to be a function that takes in the following params
    (predictions_proba, predictions_binary, labels, parameters)
    and return a numeric score
    """
    available_metrics = {
        'precision@': metrics.precision,
        'recall@': metrics.recall,
        'fbeta@': metrics.fbeta,
        'f1': metrics.f1,
        'accuracy': metrics.accuracy,
        'roc_auc': metrics.roc_auc,
        'average precision score': metrics.avg_precision,
        'true positives@': metrics.true_positives,
        'true negatives@': metrics.true_negatives,
        'false positives@': metrics.false_positives,
        'false negatives@': metrics.false_negatives,
        'fpr@': metrics.fpr,
    }

    def __init__(self, metric_groups, training_metric_groups, db_engine, sort_seed=None, custom_metrics=None):
        """
        Args:
            metric_groups (list) A list of groups of metric/configurations
                to use for evaluating all given models

                Each entry is a dict, with a list of metrics, and potentially
                    thresholds and parameter lists. Each metric is expected to
                    be a key in self.available_metrics

                Examples:

                metric_groups = [{
                    'metrics': ['precision@', 'recall@'],
                    'thresholds': {
                        'percentiles': [5.0, 10.0],
                        'top_n': [5, 10]
                    }
                }, {
                    'metrics': ['f1'],
                }, {
                    'metrics': ['fbeta@'],
                    'parameters': [{'beta': 0.75}, {'beta': 1.25}]
                }]
            training_metric_groups (list) metrics to be calculated on training set,
                in the same form as metric_groups
            db_engine (sqlalchemy.engine)
            custom_metrics (dict) Functions to generate metrics
                not available by default
                Each function is expected take in the following params:
                (predictions_proba, predictions_binary, labels, parameters)
                and return a numeric score
        """
        self.metric_groups = metric_groups
        self.training_metric_groups = training_metric_groups
        self.db_engine = db_engine
        self.sort_seed = sort_seed or int(time.time())
        if custom_metrics:
            self._validate_metrics(custom_metrics)
            self.available_metrics.update(custom_metrics)
        if self.db_engine:
            self.sessionmaker = sessionmaker(bind=self.db_engine)

    def _validate_metrics(
        self,
        custom_metrics
    ):
        for name, met in custom_metrics.items():
            if not hasattr(met, 'greater_is_better'):
                raise ValueError("Custom metric {} missing greater_is_better "
                                 "attribute".format(name))
            elif met.greater_is_better not in (True, False):
                raise ValueError("For custom metric {} greater_is_better must be "
                                 "boolean True or False".format(name))

    def _generate_evaluations(
        self,
        metrics,
        parameters,
        threshold_config,
        predictions_proba,
        predictions_binary,
        labels,
        matrix_type
    ):
        """Generate evaluations based on config and create ORM objects to hold them

        Args:
            metrics (list) names of metric to compute
            parameters (list) dicts holding parameters to pass to metrics
            threshold_config (dict) Unit type and value referring to how any
                thresholds were computed. Combined with parameter string
                to make a unique identifier for the parameter in the database
            predictions_proba (list) Probability predictions
            predictions_binary (list) Binary predictions
            labels (list) True labels
            matrix_type (string) either "Test" or "Train" for the type of matrix

        Returns: (list) results_schema.TrainEvaluation or TestEvaluation objects
        Raises: UnknownMetricError if a given metric is not present in
            self.available_metrics
        """
        evaluations = []
        num_labeled_examples = len(labels)
        num_labeled_above_threshold = predictions_binary.count(1)
        num_positive_labels = labels.count(1)
        for metric in metrics:
            if metric in self.available_metrics:
                for parameter_combination in parameters:
                    value = self.available_metrics[metric](
                        predictions_proba,
                        predictions_binary,
                        labels,
                        parameter_combination
                    )

                    full_params = parameter_combination.copy()
                    full_params.update(threshold_config)
                    parameter_string = '/'.join([
                        '{}_{}'.format(val, key)
                        for key, val in full_params.items()
                    ])
                    logging.info(
                        'Evaluations for %s%s, labeled examples %s, '
                        'above threshold %s, positive labels %s, value %s',
                        metric,
                        parameter_string,
                        num_labeled_examples,
                        num_labeled_above_threshold,
                        num_positive_labels,
                        value
                    )
                    # Most of the information to be written to the database
                    if matrix_type.lower() == "train":
                        table_obj = TrainEvaluation
                    elif matrix_type.lower() == "test":
                        table_obj = TestEvaluation

                    evaluations.append(table_obj(
                        metric=metric,
                        parameter=parameter_string,
                        value=value,
                        num_labeled_examples=num_labeled_examples,
                        num_labeled_above_threshold=num_labeled_above_threshold,
                        num_positive_labels=num_positive_labels,
                        sort_seed=self.sort_seed
                    ))
            else:
                raise metrics.UnknownMetricError()
        return evaluations

    def evaluate(
        self,
        predictions_proba,
        labels,
        model_id,
        evaluation_start_time,
        evaluation_end_time,
        as_of_date_frequency,
        matrix_type="Test"
    ):
        """Evaluate a model based on predictions, and save the results

        Args:
            predictions_proba (numpy.array) List of prediction probabilities
            labels (numpy.array) The true labels for the prediction set
            model_id (int) The database identifier of the model
            evaluation_start_time (datetime.datetime) The time of the
                first prediction being evaluated
            evaluation_end_time (datetime.datetime) The time of the last prediction being evaluated
            as_of_date_frequency (string) How frequently predictions were generated
            matrix_type (string) either "Test" or "Train" for the type of matrix
        """

        logging.info(
            'Generating evaluations for model id %s, evaluation range %s-%s, '
            'as_of_date frequency %s',
            model_id,
            evaluation_start_time,
            evaluation_end_time,
            as_of_date_frequency
        )
        predictions_proba_sorted, labels_sorted = sort_predictions_and_labels(
            predictions_proba,
            labels,
            self.sort_seed
        )
        labels_sorted = numpy.array(labels_sorted)

        evaluations = []
        if matrix_type.lower() == "train":
            metrics_groups_to_compute = self.training_metric_groups
        elif matrix_type.lower() == "test":
            metrics_groups_to_compute = self.metric_groups
        else:
            raise ValueError("metric set {} unrecognized. Please select 'Train' or 'Test'".format(matrix_type))

        for group in metrics_groups_to_compute:
            logging.info('Creating evaluations for metric group %s', group)
            parameters = group.get('parameters', [{}])
            if 'thresholds' not in group:
                logging.info('Not a thresholded group, generating evaluation '
                             'based on all predictions')
                evaluations = evaluations + self._generate_evaluations(
                    group['metrics'],
                    parameters,
                    {},
                    predictions_proba,
                    generate_binary_at_x(
                        predictions_proba_sorted,
                        100,
                        unit='percentile'
                    ),
                    labels_sorted.tolist(),
                    matrix_type
                )

            for pct_thresh in group.get('thresholds', {}).get('percentiles', []):
                logging.info('Processing percent threshold %s', pct_thresh)
                predicted_classes = numpy.array(generate_binary_at_x(
                    predictions_proba_sorted,
                    pct_thresh,
                    unit='percentile'
                ))
                nan_mask = numpy.isfinite(labels_sorted)
                predicted_classes = (predicted_classes[nan_mask]).tolist()
                present_labels_sorted = (labels_sorted[nan_mask]).tolist()
                evaluations = evaluations + self._generate_evaluations(
                    group['metrics'],
                    parameters,
                    {'pct': pct_thresh},
                    None,
                    predicted_classes,
                    present_labels_sorted,
                    matrix_type
                )

            for abs_thresh in group.get('thresholds', {}).get('top_n', []):
                logging.info('Processing absolute threshold %s', abs_thresh)
                predicted_classes = numpy.array(generate_binary_at_x(
                    predictions_proba_sorted,
                    abs_thresh,
                    unit='top_n'
                ))
                nan_mask = numpy.isfinite(labels_sorted)
                predicted_classes = (predicted_classes[nan_mask]).tolist()
                present_labels_sorted = (labels_sorted[nan_mask]).tolist()
                evaluations = evaluations + self._generate_evaluations(
                    group['metrics'],
                    parameters,
                    {'abs': abs_thresh},
                    None,
                    predicted_classes,
                    present_labels_sorted,
                    matrix_type
                )

        logging.info('Writing metrics to db: %s table', matrix_type)
        self._write_to_db(
            model_id,
            evaluation_start_time,
            evaluation_end_time,
            as_of_date_frequency,
            evaluations,
            matrix_type
        )
        logging.info('Done writing metrics to db: %s table', matrix_type)

    @db_retry
    def _write_to_db(
        self,
        model_id,
        evaluation_start_time,
        evaluation_end_time,
        as_of_date_frequency,
        evaluations,
        matrix_type
    ):
        """Write evaluation objects to the database

        Binds the model_id as as_of_date to the given ORM objects
        and writes them to the database

        Args:
            model_id (int) primary key of the model
            as_of_date (datetime.date) Date the predictions were made as of
            evaluations (list) results_schema.Evaluation objects
            matrix_type (string) Train or Test, specifies to which table to write
        """
        session = self.sessionmaker()
        if matrix_type.lower() == "train":
            table_obj = TrainEvaluation
        elif matrix_type.lower() == "test":
            table_obj = TestEvaluation

        session.query(table_obj)\
            .filter_by(
                model_id=model_id,
                evaluation_start_time=evaluation_start_time,
                evaluation_end_time=evaluation_end_time,
                as_of_date_frequency=as_of_date_frequency
            ).delete()

        for evaluation in evaluations:
            evaluation.model_id = model_id
            evaluation.evaluation_start_time = evaluation_start_time
            evaluation.evaluation_end_time = evaluation_end_time
            evaluation.as_of_date_frequency = as_of_date_frequency
            session.add(evaluation)
        session.commit()
        session.close()
