import io
import logging

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

logger = logging.getLogger(__name__)


class GoogleDriveManager:
    """
    Google Drive storage manager for test cases.

    Stores files using the required structure:

    test_cases/{problem_id}/{test_number}
    test_cases/{problem_id}/{test_number}.a
    """

    SCOPES = ["https://www.googleapis.com/auth/drive"]

    def __init__(self, credentials_file, root_folder_id):
        self.root_folder_id = root_folder_id

        credentials = service_account.Credentials.from_service_account_file(
            credentials_file,
            scopes=self.SCOPES
        )

        self.service = build(
            "drive",
            "v3",
            credentials=credentials
        )

        logger.info("Google Drive connection initialized successfully.")

    def _find_folder(self, name, parent_id):
        query = (
            "name = '{name}' "
            "and mimeType = 'application/vnd.google-apps.folder' "
            "and '{parent_id}' in parents "
            "and trashed = false"
        ).format(name=name.replace("'", "\\'"), parent_id=parent_id)

        response = self.service.files().list(
            q=query,
            spaces="drive",
            fields="files(id, name)"
        ).execute()

        files = response.get("files", [])

        return files[0]["id"] if files else None

    def _create_folder(self, name, parent_id):
        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id]
        }

        folder = self.service.files().create(
            body=metadata,
            fields="id"
        ).execute()

        return folder["id"]

    def _get_or_create_folder(self, name, parent_id):
        folder_id = self._find_folder(name, parent_id)

        if folder_id:
            return folder_id

        return self._create_folder(name, parent_id)

    def _upload_file(self, name, content, parent_id):
        existing_query = (
            "name = '{name}' "
            "and '{parent_id}' in parents "
            "and trashed = false"
        ).format(name=name.replace("'", "\\'"), parent_id=parent_id)

        existing = self.service.files().list(
            q=existing_query,
            spaces="drive",
            fields="files(id)"
        ).execute().get("files", [])

        media = MediaIoBaseUpload(
            io.BytesIO(content.encode("utf-8")),
            mimetype="text/plain",
            resumable=False
        )

        metadata = {
            "name": name,
            "parents": [parent_id]
        }

        if existing:
            file_id = existing[0]["id"]

            self.service.files().update(
                fileId=file_id,
                media_body=media
            ).execute()

            return file_id

        created = self.service.files().create(
            body=metadata,
            media_body=media,
            fields="id"
        ).execute()

        return created["id"]

    def upload_test_case(
        self,
        container_name,
        db_problem_id,
        test_number,
        input_data,
        output_data
    ):
        """
        Upload input and output files using the required structure.
        """

        problem_folder = self._get_or_create_folder(
            str(db_problem_id),
            self._get_or_create_folder(
                "test_cases",
                self.root_folder_id
            )
        )

        input_name = f"{test_number:02d}"
        output_name = f"{test_number:02d}.a"

        self._upload_file(
            input_name,
            input_data,
            problem_folder
        )

        self._upload_file(
            output_name,
            output_data,
            problem_folder
        )

        logger.info(
            f"Uploaded test case #{test_number} "
            f"for problem {db_problem_id} to Google Drive."
        )

    def empty_blob(self, container_name, problem_id):
        """
        Deletes all test-case files for a problem.
        """

        test_cases_folder = self._find_folder(
            "test_cases",
            self.root_folder_id
        )

        if not test_cases_folder:
            return

        problem_folder = self._find_folder(
            str(problem_id),
            test_cases_folder
        )

        if not problem_folder:
            return

        query = (
            f"'{problem_folder}' in parents "
            "and trashed = false"
        )

        files = self.service.files().list(
            q=query,
            spaces="drive",
            fields="files(id, name)"
        ).execute().get("files", [])

        for file in files:
            self.service.files().delete(
                fileId=file["id"]
            ).execute()

        self.service.files().delete(
            fileId=problem_folder
        ).execute()

        logger.info(
            f"Deleted Google Drive test cases for problem {problem_id}."
        )