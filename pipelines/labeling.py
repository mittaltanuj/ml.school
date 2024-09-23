import logging
import sys

from common import PYTHON, packages
from metaflow import (
    FlowSpec,
    Parameter,
    project,
    pypi_base,
    step,
)
from sagemaker import load_unlabeled_data

logger = logging.getLogger(__name__)


@project(name="penguins")
@pypi_base(
    python=PYTHON,
    packages=packages("pandas", "numpy", "boto3"),
)
class Labeling(FlowSpec):
    """A labeling pipeline to automatically generate fake ground truth labels.

    This pipeline generates fake labels for any data collected by a hosted model.
    Of course, this is only useful for testing the monitoring process. In a production
    environment, you would need an actual labeling process (manual or automatic) to
    generate ground truth data.
    """

    datastore_uri = Parameter(
        "datastore-uri",
        help=(
            "The location where the data collected by the hosted model is stored. This "
            "pipeline supports using data stored in a SQLite database, or data "
            "captured by a SageMaker endpoint and stored in S3."
        ),
        required=True,
    )

    ground_truth_uri = Parameter(
        "ground-truth-uri",
        help=(
            "When labeling data captured by a SageMaker endpoint, this parameter "
            "specifies the S3 location where the ground truth labels are stored. "
        ),
        required=False,
    )

    ground_truth_quality = Parameter(
        "ground-truth-quality",
        help=(
            "This parameter represents how similar the ground truth labels will be "
            "to the predictions generated by the model. Setting this parameter to a "
            "value less than 1.0 will introduce noise in the labels to simulate "
            "inaccurate model predictions."
        ),
        default=0.8,
    )

    @step
    def start(self):
        """Generate ground truth labels for unlabeled data captured by the model."""
        if self.datastore_uri.startswith("sqlite://"):
            self.labeled_samples = self._label_sqlite_data()
        elif self.datastore_uri.startswith("s3://"):
            self.labeled_samples = self._label_sagemaker_data()
        else:
            message = (
                "Invalid datastore location. Must be an S3 location in the "
                "format 's3://bucket/prefix' or a SQLite database file in the format "
                "'sqlite://path/to/database.db'"
            )
            raise ValueError(message)

        self.next(self.end)

    @step
    def end(self):
        """End of the pipeline."""
        logger.info("Labeled %s samples.", self.labeled_samples)

    def _get_label(self, prediction):
        """Generate a fake ground truth label for a sample.

        This function will randomly return a ground truth label taking into account the
        prediction quality we want to achieve.
        """
        import random

        return (
            prediction
            if random.random() < self.ground_truth_quality
            else random.choice(["Adelie", "Chinstrap", "Gentoo"])
        )

    def _label_sqlite_data(self):
        """Generate ground truth labels for data captured by a local inference service.

        This function loads any unlabeled data from the SQLite database where the data
        was stored by the model and generates fake ground truth labels for it.
        """
        import sqlite3

        import pandas as pd

        connection = sqlite3.connect(self.datastore_uri)

        # We want to return any unlabeled samples from the database.
        df = pd.read_sql_query("SELECT * FROM data WHERE species IS NULL", connection)
        logger.info("Loaded %s unlabeled samples from the database.", len(df))

        # If there are no unlabeled samples, we don't need to do anything else.
        if df.empty:
            return 0

        for _, row in df.iterrows():
            uuid = row["uuid"]
            label = self._get_label(row["prediction"])

            # Update the database
            update_query = "UPDATE data SET species = ? WHERE uuid = ?"
            connection.execute(update_query, (label, uuid))

        connection.commit()
        connection.close()

        return len(df)

    def _label_sagemaker_data(self):
        """Generate ground truth labels for data captured by a SageMaker endpoint.

        This function loads any unlabeled data from the location where SageMaker stores
        the data captured by the endpoint and generates fake ground truth labels. The
        function stores the labels in the specified S3 location.
        """
        import json
        from datetime import datetime, timezone

        import boto3

        if not self.ground_truth_uri:
            message = "The 'ground-truth-uri' parameter is required."
            raise RuntimeError(message)

        s3_client = boto3.client("s3")

        data = load_unlabeled_data(
            s3_client,
            self.datastore_uri,
            self.ground_truth_uri,
        )

        logger.info("Loaded %s unlabeled samples from S3.", len(data))

        # If there are no unlabeled samples, we don't need to do anything else.
        if data.empty:
            return 0

        records = []

        for event_id, group in data.groupby("event_id"):
            predictions = []
            for _, row in group.iterrows():
                predictions.append(self._get_label(row["prediction"]))

            record = {
                "groundTruthData": {
                    # For testing purposes, we will generate a random
                    # label for each request.
                    "data": predictions,
                    "encoding": "CSV",
                },
                "eventMetadata": {
                    # This value should match the id of the request
                    # captured by the endpoint.
                    "eventId": event_id,
                },
                "eventVersion": "0",
            }

            records.append(json.dumps(record))

        ground_truth_payload = "\n".join(records)
        upload_time = datetime.now(tz=timezone.utc)
        uri = (
            "/".join(self.ground_truth_uri.split("/")[3:])
            + f"{upload_time:%Y/%m/%d/%H/%M%S}.jsonl"
        )

        s3_client.put_object(
            Body=ground_truth_payload,
            Bucket=self.ground_truth_uri.split("/")[2],
            Key=uri,
        )

        return len(data)


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        level=logging.INFO,
    )
    logging.getLogger("botocore.credentials").setLevel(logging.ERROR)
    Labeling()
