import pytest
import uuid
from unittest.mock import patch, MagicMock
import pandas as pd

import g3dt.indexd.indexd_registrar as registrar


# --- Fixtures ---

@pytest.fixture
def single_file_df():
    """Provides a one-row DataFrame representing a single S3 file."""
    return pd.DataFrame(
        [
            {
                "file_name": "test.csv",
                "md5": "abc123",
                "file_size": 100,
                "s3_url": "s3://bucket/data/test.csv",
                "baseid": registrar.filename_to_baseid("test.csv"),
            }
        ]
    )


@pytest.fixture
def multi_file_df():
    """Provides a three-row DataFrame representing multiple S3 files."""
    rows = []
    for i in range(1, 4):
        name = f"file_{i}.csv"
        rows.append(
            {
                "file_name": name,
                "md5": f"md5hash{i}",
                "file_size": i * 1000,
                "s3_url": f"s3://bucket/data/{name}",
                "baseid": registrar.filename_to_baseid(name),
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture
def mock_gen3_index():
    """Provides a mocked Gen3Index client."""
    return MagicMock()


# --- Tests for filename_to_baseid ---

class TestFilenameToBaseid:
    """Test suite for deterministic UUIDv5 baseid generation from filenames."""

    def test_deterministic_baseid(self):
        """
        The baseid for a given filename must always be the same value.

        This is critical because indexd uses baseid to group versions of
        the same logical file.  If baseid were random, re-processing the
        same file would create orphan records instead of linking versions.

        Input:  filename = "test.txt"
        Output: "56f1fdc0-df48-5245-9910-75a0cfb5c641"
                (the known UUIDv5 of "test.txt" under NAMESPACE_DNS)
        """
        expected = "56f1fdc0-df48-5245-9910-75a0cfb5c641"
        assert registrar.filename_to_baseid("test.txt") == expected
        # Call again to prove determinism
        assert registrar.filename_to_baseid("test.txt") == expected

    def test_different_filenames_different_baseids(self):
        """
        Different filenames must produce different baseids so that
        distinct files are never accidentally treated as versions of
        each other in indexd.

        Input:  "file_a.csv", "file_b.csv"
        Output: baseid_a != baseid_b
        """
        baseid_a = registrar.filename_to_baseid("file_a.csv")
        baseid_b = registrar.filename_to_baseid("file_b.csv")
        assert baseid_a != baseid_b

    def test_baseid_is_valid_uuid(self):
        """
        The returned string must be a valid UUID so that it is accepted
        by the indexd API, which expects a UUID-formatted baseid.

        Input:  filename = "edcad-lipid-total.csv"
        Output: uuid.UUID(result) does not raise
        """
        result = registrar.filename_to_baseid("edcad-lipid-total.csv")
        parsed = uuid.UUID(result)
        assert str(parsed) == result

    def test_baseid_ignores_path(self):
        """
        The function takes a bare filename, not a path.  Callers are
        responsible for extracting the basename before calling.  This
        test confirms that passing "test.txt" yields the known baseid,
        matching the value from the linking_indexd notebook.

        Input:  "test.txt"
        Output: "56f1fdc0-df48-5245-9910-75a0cfb5c641"
        """
        expected = "56f1fdc0-df48-5245-9910-75a0cfb5c641"
        assert registrar.filename_to_baseid("test.txt") == expected


# --- Tests for scan_s3_files ---

class TestScanS3Files:
    """Test suite for recursive S3 file scanning and metadata collection."""

    @patch("g3dt.indexd.indexd_registrar.wr")
    def test_scan_single_path(self, mock_wr):
        """
        Scanning one S3 prefix should list all objects, call HeadObject
        for each, and return a DataFrame with one row per file containing
        file_name, md5 (from ETag), file_size, s3_url, and baseid.

        Input:  s3_paths = ["s3://bucket/data/"]
                list_objects returns two files
                head_object returns ETag='"abc123"', ContentLength=1024
        Output: DataFrame with 2 rows and correct column values
        """
        mock_wr.s3.list_objects.return_value = [
            "s3://bucket/data/file1.csv",
            "s3://bucket/data/file2.csv",
        ]

        mock_session = MagicMock()
        mock_s3_client = MagicMock()
        mock_session.client.return_value = mock_s3_client
        mock_s3_client.head_object.return_value = {
            "ETag": '"abc123"',
            "ContentLength": 1024,
        }

        df = registrar.scan_s3_files(
            ["s3://bucket/data/"], boto3_session=mock_session
        )

        assert len(df) == 2
        assert list(df.columns) == [
            "file_name", "md5", "file_size", "s3_url", "baseid",
        ]
        assert df.iloc[0]["file_name"] == "file1.csv"
        assert df.iloc[1]["file_name"] == "file2.csv"
        assert df.iloc[0]["md5"] == "abc123"
        assert df.iloc[0]["file_size"] == 1024
        assert df.iloc[0]["s3_url"] == "s3://bucket/data/file1.csv"
        assert (
            df.iloc[0]["baseid"]
            == registrar.filename_to_baseid("file1.csv")
        )

    @patch("g3dt.indexd.indexd_registrar.wr")
    def test_scan_multiple_paths(self, mock_wr):
        """
        When multiple S3 prefixes are provided, files from all prefixes
        should be concatenated into a single DataFrame.

        Input:  s3_paths = ["s3://bucket/path1/", "s3://bucket/path2/"]
                1 file in each prefix
        Output: DataFrame with 2 rows
        """
        mock_wr.s3.list_objects.side_effect = [
            ["s3://bucket/path1/a.csv"],
            ["s3://bucket/path2/b.csv"],
        ]

        mock_session = MagicMock()
        mock_s3_client = MagicMock()
        mock_session.client.return_value = mock_s3_client
        mock_s3_client.head_object.return_value = {
            "ETag": '"xyz"',
            "ContentLength": 512,
        }

        df = registrar.scan_s3_files(
            ["s3://bucket/path1/", "s3://bucket/path2/"],
            boto3_session=mock_session,
        )

        assert len(df) == 2
        assert set(df["file_name"]) == {"a.csv", "b.csv"}

    @patch("g3dt.indexd.indexd_registrar.wr")
    def test_scan_empty_path(self, mock_wr):
        """
        An S3 prefix with no objects should return an empty DataFrame
        with the expected columns, so downstream code can safely operate
        on the result without key errors.

        Input:  s3_paths = ["s3://bucket/empty/"]
                list_objects returns []
        Output: empty DataFrame with columns
                [file_name, md5, file_size, s3_url, baseid]
        """
        mock_wr.s3.list_objects.return_value = []

        mock_session = MagicMock()
        mock_session.client.return_value = MagicMock()

        df = registrar.scan_s3_files(
            ["s3://bucket/empty/"], boto3_session=mock_session
        )

        assert len(df) == 0
        assert list(df.columns) == [
            "file_name", "md5", "file_size", "s3_url", "baseid",
        ]

    @patch("g3dt.indexd.indexd_registrar.wr")
    def test_scan_strips_etag_quotes(self, mock_wr):
        """
        S3 ETags are returned wrapped in double-quotes (e.g. '"abc"').
        The md5 column must have these quotes stripped so the hash is
        usable directly in indexd registration payloads.

        Input:  ETag = '"d41d8cd98f00b204e9800998ecf8427e"'
        Output: md5 = "d41d8cd98f00b204e9800998ecf8427e"
        """
        mock_wr.s3.list_objects.return_value = [
            "s3://bucket/data/file.csv",
        ]

        mock_session = MagicMock()
        mock_s3_client = MagicMock()
        mock_session.client.return_value = mock_s3_client
        mock_s3_client.head_object.return_value = {
            "ETag": '"d41d8cd98f00b204e9800998ecf8427e"',
            "ContentLength": 0,
        }

        df = registrar.scan_s3_files(
            ["s3://bucket/data/"], boto3_session=mock_session
        )

        assert df.iloc[0]["md5"] == "d41d8cd98f00b204e9800998ecf8427e"


# --- Tests for register_files_with_indexd ---

class TestRegisterFilesWithIndexd:
    """Test suite for indexd registration logic."""

    def test_register_single_file(
        self, single_file_df, mock_gen3_index
    ):
        """
        Registering one file should call create_record with the correct
        payload and return a DataFrame with did, rev, and registered_at
        columns appended.

        Input:  1-row DataFrame (file_name="test.csv", md5="abc123",
                file_size=100, s3_url="s3://bucket/data/test.csv")
                authz=["/programs/program1/projects/EDCAD-PMS"]
                create_record returns
                    {"did": "PREFIX/did-1", "baseid": "uuid-1",
                     "rev": "aabb"}
        Output: DataFrame with 1 row; did="PREFIX/did-1", rev="aabb"
        """
        mock_gen3_index.get_record.side_effect = Exception("not found")
        mock_gen3_index.create_record.return_value = {
            "did": "PREFIX/did-1",
            "baseid": single_file_df.iloc[0]["baseid"],
            "rev": "aabb",
        }

        authz = ["/programs/program1/projects/EDCAD-PMS"]
        result = registrar.register_files_with_indexd(
            mock_gen3_index, single_file_df, authz
        )

        assert len(result) == 1
        assert result.iloc[0]["did"] == "PREFIX/did-1"
        assert result.iloc[0]["rev"] == "aabb"
        assert "registered_at" in result.columns

        mock_gen3_index.create_record.assert_called_once_with(
            hashes={"md5": "abc123"},
            size=100,
            urls=["s3://bucket/data/test.csv"],
            urls_metadata={"s3://bucket/data/test.csv": {}},
            file_name="test.csv",
            baseid=single_file_df.iloc[0]["baseid"],
            authz=authz,
        )

    def test_register_multiple_files(
        self, multi_file_df, mock_gen3_index
    ):
        """
        Bulk registration should produce one did per file, each with a
        unique identifier from indexd.

        Input:  3-row DataFrame
                create_record returns distinct dids
        Output: DataFrame with 3 rows, each with a unique did
        """
        mock_gen3_index.get_record.side_effect = Exception("not found")
        mock_gen3_index.create_record.side_effect = [
            {"did": f"PREFIX/did-{i}", "baseid": f"b-{i}", "rev": f"r-{i}"}
            for i in range(1, 4)
        ]

        result = registrar.register_files_with_indexd(
            mock_gen3_index, multi_file_df, ["/programs/program1/projects/X"]
        )

        assert len(result) == 3
        assert list(result["did"]) == [
            "PREFIX/did-1", "PREFIX/did-2", "PREFIX/did-3",
        ]

    def test_register_always_uploads(self, mock_gen3_index):
        """
        All files should be submitted to indexd regardless of whether
        the baseid already exists.  Re-submitting the same baseid
        creates a new revision in indexd, so every file produces a
        result row.

        Input:  2-row DataFrame; create_record succeeds for both rows
        Output: DataFrame with 2 rows, create_record called twice
        """
        df = pd.DataFrame(
            [
                {
                    "file_name": "existing.csv",
                    "md5": "aaa",
                    "file_size": 10,
                    "s3_url": "s3://b/existing.csv",
                    "baseid": "existing-baseid",
                },
                {
                    "file_name": "new.csv",
                    "md5": "bbb",
                    "file_size": 20,
                    "s3_url": "s3://b/new.csv",
                    "baseid": "new-baseid",
                },
            ]
        )

        mock_gen3_index.create_record.side_effect = [
            {
                "did": "PREFIX/did-1",
                "baseid": "existing-baseid",
                "rev": "rev1",
            },
            {
                "did": "PREFIX/did-2",
                "baseid": "new-baseid",
                "rev": "rev1",
            },
        ]

        result = registrar.register_files_with_indexd(
            mock_gen3_index, df, ["/programs/program1/projects/X"]
        )

        assert len(result) == 2
        assert result.iloc[0]["did"] == "PREFIX/did-1"
        assert result.iloc[1]["did"] == "PREFIX/did-2"
        assert mock_gen3_index.create_record.call_count == 2

    def test_register_handles_api_error(
        self, mock_gen3_index
    ):
        """
        If indexd returns an error for one file, that file should be
        skipped and the remaining files should still be registered.
        This prevents a single bad record from failing the entire batch.

        Input:  2-row DataFrame; create_record raises on row 0,
                succeeds on row 1
        Output: DataFrame with 1 successful row
        """
        df = pd.DataFrame(
            [
                {
                    "file_name": "bad.csv",
                    "md5": "aaa",
                    "file_size": 10,
                    "s3_url": "s3://b/bad.csv",
                    "baseid": "baseid-bad",
                },
                {
                    "file_name": "good.csv",
                    "md5": "bbb",
                    "file_size": 20,
                    "s3_url": "s3://b/good.csv",
                    "baseid": "baseid-good",
                },
            ]
        )

        mock_gen3_index.get_record.side_effect = Exception("not found")
        mock_gen3_index.create_record.side_effect = [
            Exception("API error"),
            {"did": "PREFIX/good-did", "baseid": "baseid-good", "rev": "r1"},
        ]

        result = registrar.register_files_with_indexd(
            mock_gen3_index, df, ["/programs/program1/projects/X"]
        )

        assert len(result) == 1
        assert result.iloc[0]["file_name"] == "good.csv"
        assert result.iloc[0]["did"] == "PREFIX/good-did"


# --- Tests for write_to_glue ---

class TestWriteToGlue:
    """Test suite for Iceberg table write operations."""

    @patch(
        "g3dt.indexd.indexd_registrar.write_iceberg_to_db"
    )
    def test_write_calls_iceberg_correctly(self, mock_write):
        """
        Ensures write_iceberg_to_db is called with the correct database,
        table, DataFrame, and partition_cols so that indexd registration
        results end up in the right Glue Iceberg table with the correct
        partitioning scheme.

        Input:  2-row DataFrame, database="db", table="indexd_registry",
                s3_path="s3://bucket/path/",
                partition_cols=["study_id", "indexd_endpoint"]
        Output: write_iceberg_to_db called once with matching arguments
        """
        df = pd.DataFrame(
            [
                {"file_name": "a.csv", "did": "did-1"},
                {"file_name": "b.csv", "did": "did-2"},
            ]
        )

        registrar.write_to_glue(
            df=df,
            database="db",
            table="indexd_registry",
            athena_s3_output="s3://bucket/output/",
            table_location="s3://bucket/path/",
            partition_cols=["study_id", "indexd_endpoint"],
        )

        mock_write.assert_called_once_with(
            df=df,
            database="db",
            table="indexd_registry",
            athena_s3_output="s3://bucket/output/",
            workgroup="primary",
            table_location="s3://bucket/path/",
            partition_cols=["study_id", "indexd_endpoint"],
            merge_cols=None,
            schema_evolution=False,
            boto3_session=None,
        )
