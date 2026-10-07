from django.conf import settings
from .googledrivetestcase import GoogleDriveManager


def get_storage_manager():
    provider = getattr(settings, "STORAGE_PROVIDER", "gdrive").lower()

    if provider == "gdrive":
        return GoogleDriveManager(
            credentials_file=settings.GOOGLE_DRIVE_CREDENTIALS_FILE,
            root_folder_id=settings.GOOGLE_DRIVE_FOLDER_ID,
        )

    raise ValueError(
        f"Unsupported storage provider: {provider}. "
        "Currently supported: gdrive"
    )