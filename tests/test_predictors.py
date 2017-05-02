import boto3
import testing.postgresql

from moto import mock_s3
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.session import make_transient
from triage.db import ensure_db, Prediction
import pandas

from triage.predictors import Predictor
from tests.utils import fake_trained_model
from triage.storage import \
    InMemoryModelStorageEngine,\
    S3ModelStorageEngine,\
    InMemoryMatrixStore
import datetime

from unittest.mock import Mock
from numpy.testing import assert_array_equal

AS_OF_DATE = datetime.date(2016, 12, 21)


def test_predictor():
    with testing.postgresql.Postgresql() as postgresql:
        db_engine = create_engine(postgresql.url())
        ensure_db(db_engine)

        with mock_s3():
            s3_conn = boto3.resource('s3')
            s3_conn.create_bucket(Bucket='econ-dev')
            project_path = 'econ-dev/inspections'
            model_storage_engine = S3ModelStorageEngine(s3_conn, project_path)
            _, model_id = \
                fake_trained_model(project_path, model_storage_engine, db_engine)
            predictor = Predictor(project_path, model_storage_engine, db_engine)
            # create prediction set
            matrix = pandas.DataFrame.from_dict({
                'entity_id': [1, 2],
                'feature_one': [3, 4],
                'feature_two': [5, 6],
                'label': [7, 8]
            }).set_index('entity_id')
            metadata = {
                'label_name': 'label',
                'end_time': AS_OF_DATE,
                'metta-uuid': '1234',
            }

            matrix_store = InMemoryMatrixStore(matrix, metadata)
            predict_proba = predictor.predict(model_id, matrix_store, misc_db_parameters=dict())

            # assert
            # 1. that the returned predictions are of the desired length
            assert len(predict_proba) == 2

            # 2. that the predictions table entries are present and
            # can be linked to the original models
            records = [
                row for row in
                db_engine.execute('''select entity_id, as_of_date
                from results.predictions
                join results.models using (model_id)''')
            ]
            assert len(records) == 2

            # 3. that the contained as_of_dates match what we sent in
            for record in records:
                assert record[1].date() == AS_OF_DATE

            # 4. that the entity ids match the given dataset
            assert sorted([record[0] for record in records]) == [1, 2]

            # 5. running with same model_id, different as of date
            # then with same as of date only replaces the records
            # with the same date
            new_matrix = pandas.DataFrame.from_dict({
                'entity_id': [1, 2],
                'feature_one': [3, 4],
                'feature_two': [5, 6],
                'label': [7, 8]
            }).set_index('entity_id')
            new_metadata = {
                'label_name': 'label',
                'end_time': AS_OF_DATE + datetime.timedelta(days=1),
                'metta-uuid': '1234',
            }
            new_matrix_store = InMemoryMatrixStore(new_matrix, new_metadata)
            predictor.predict(model_id, new_matrix_store, misc_db_parameters=dict())
            predictor.predict(model_id, matrix_store, misc_db_parameters=dict())
            records = [
                row for row in
                db_engine.execute('''select entity_id, as_of_date
                from results.predictions
                join results.models using (model_id)''')
            ]
            assert len(records) == 4

            # 6. That we can delete the model when done prediction on it
            predictor.delete_model(model_id)
            assert predictor.load_model(model_id) == None


def test_predictor_composite_index():
    with testing.postgresql.Postgresql() as postgresql:
        db_engine = create_engine(postgresql.url())
        ensure_db(db_engine)
        project_path = 'econ-dev/inspections'
        model_storage_engine = InMemoryModelStorageEngine(project_path)
        _, model_id = \
            fake_trained_model(project_path, model_storage_engine, db_engine)
        predictor = Predictor(project_path, model_storage_engine, db_engine)
        dayone = datetime.datetime(2011, 1, 1)
        daytwo = datetime.datetime(2011, 1, 2)
        # create prediction set
        matrix = pandas.DataFrame.from_dict({
            'entity_id': [1, 2, 1, 2],
            'as_of_date': [dayone, dayone, daytwo, daytwo],
            'feature_one': [3, 4, 5, 6],
            'feature_two': [5, 6, 7, 8],
            'label': [7, 8, 8, 7]
        }).set_index(['entity_id', 'as_of_date'])
        metadata = {
            'label_name': 'label',
            'end_time': AS_OF_DATE,
            'metta-uuid': '1234',
        }
        matrix_store = InMemoryMatrixStore(matrix, metadata)
        predict_proba = predictor.predict(model_id, matrix_store, misc_db_parameters=dict())

        # assert
        # 1. that the returned predictions are of the desired length
        assert len(predict_proba) == 4

        # 2. that the predictions table entries are present and
        # can be linked to the original models
        records = [
            row for row in
            db_engine.execute('''select entity_id, as_of_date
            from results.predictions
            join results.models using (model_id)''')
        ]
        assert len(records) == 4

def test_predictor_retrieve():
    with testing.postgresql.Postgresql() as postgresql:
        db_engine = create_engine(postgresql.url())
        ensure_db(db_engine)
        project_path = 'econ-dev/inspections'
        model_storage_engine = InMemoryModelStorageEngine(project_path)
        _, model_id = \
            fake_trained_model(project_path, model_storage_engine, db_engine)
        predictor = Predictor(project_path, model_storage_engine, db_engine, replace=False)
        dayone = datetime.date(2011, 1, 1).isoformat()
        daytwo = datetime.date(2011, 1, 2).isoformat()
        # create prediction set
        matrix_data = {
            'entity_id': [1, 2, 1, 2],
            'as_of_date': [dayone, dayone, daytwo, daytwo],
            'feature_one': [3, 4, 5, 6],
            'feature_two': [5, 6, 7, 8],
            'label': [7, 8, 8, 7]
        }
        matrix = pandas.DataFrame.from_dict(matrix_data)\
            .set_index(['entity_id', 'as_of_date'])
        metadata = {
            'label_name': 'label',
            'end_time': AS_OF_DATE,
            'metta-uuid': '1234',
        }
        matrix_store = InMemoryMatrixStore(matrix, metadata)
        predict_proba = predictor.predict(model_id, matrix_store, misc_db_parameters=dict())

        # When run again, the predictions retrieved from the database
        # should match.
        #
        # Some trickiness here. Let's explain:
        #
        # If we are not careful, retrieving predictions from the database and
        # presenting them as a numpy array can result in a bad ordering,
        # since the given matrix may not be 'ordered' by some criteria
        # that can be easily represented by an ORDER BY clause.
        #
        # It will sometimes work, because without ORDER BY you will get
        # it back in the table's physical order, which unless something has
        # happened to the table will be the order you inserted it,
        # which could very well be the order in the matrix.
        # So it's not a bug that would necessarily immediately show itself,
        # but when it does go wrong your scores will be garbage.
        #
        # So we simulate a table order mutation that can happen over time:
        # Remove the first row and put it at the end.
        # If the Predictor doesn't explicitly reorder the results, this will fail
        session = sessionmaker(bind=db_engine)()
        obj = session.query(Prediction).first()
        session.delete(obj)
        session.commit()

        make_transient(obj)
        session = sessionmaker(bind=db_engine)()
        session.add(obj)
        session.commit()

        predictor.load_model = Mock()
        new_predict_proba = predictor.predict(model_id, matrix_store, misc_db_parameters=dict())
        assert_array_equal(new_predict_proba, predict_proba)
        assert not predictor.load_model.called
