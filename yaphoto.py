"""Backward-compatible public helpers and the YaPhoto CLI entry point."""

import os as os
import time as time

import requests as requests

from yaphoto_lib.api import BASE as BASE
from yaphoto_lib.api import Cooldown as Cooldown
from yaphoto_lib.api import Yandex as Yandex
from yaphoto_lib.api import check_response as check_response
from yaphoto_lib.api import cookies_from_file as cookies_from_file
from yaphoto_lib.api import request as request
from yaphoto_lib.api import retry_after as retry_after
from yaphoto_lib.api import transfer as transfer
from yaphoto_lib.api import validate_url as validate_url
from yaphoto_lib.catalog import IMAGE_EXT as IMAGE_EXT
from yaphoto_lib.catalog import VIDEO_EXT as VIDEO_EXT
from yaphoto_lib.catalog import allocate_relpath as allocate_relpath
from yaphoto_lib.catalog import bind_account as bind_account
from yaphoto_lib.catalog import collect_list as collect_list
from yaphoto_lib.catalog import collect_photoslice as collect_photoslice
from yaphoto_lib.catalog import connect as connect
from yaphoto_lib.catalog import flatten_layout as flatten_layout
from yaphoto_lib.catalog import is_photo as is_photo
from yaphoto_lib.catalog import local_relpath as local_relpath
from yaphoto_lib.catalog import media_kind as media_kind
from yaphoto_lib.catalog import normalize_preview_names as normalize_preview_names
from yaphoto_lib.catalog import remove_redundant_preview as remove_redundant_preview
from yaphoto_lib.catalog import report as report
from yaphoto_lib.catalog import save_resource as save_resource
from yaphoto_lib.catalog import scan as scan
from yaphoto_lib.cli import main as main
from yaphoto_lib.console import Console as Console
from yaphoto_lib.console import ServerStats as ServerStats
from yaphoto_lib.console import log_event as log_event
from yaphoto_lib.engine import PREVIEW_RANK as PREVIEW_RANK
from yaphoto_lib.engine import download as download
from yaphoto_lib.engine import download_one as download_one
from yaphoto_lib.engine import save_preview as save_preview
from yaphoto_lib.engine import try_preview as try_preview
from yaphoto_lib.engine import verify as verify
from yaphoto_lib.errors import AuthError as AuthError
from yaphoto_lib.errors import ExportError as ExportError
from yaphoto_lib.errors import IntegrityError as IntegrityError
from yaphoto_lib.errors import RemoteError as RemoteError
from yaphoto_lib.errors import StorageUnavailable as StorageUnavailable
from yaphoto_lib.files import apply_dates as apply_dates
from yaphoto_lib.files import digests as digests
from yaphoto_lib.files import file_fingerprint as file_fingerprint
from yaphoto_lib.files import parse_time as parse_time
from yaphoto_lib.files import store_fingerprint as store_fingerprint
from yaphoto_lib.files import validate_file as validate_file
from yaphoto_lib.options import DEFAULT_ATTEMPTS as DEFAULT_ATTEMPTS
from yaphoto_lib.options import DEFAULT_READ_TIMEOUT as DEFAULT_READ_TIMEOUT
from yaphoto_lib.options import DownloadOptions as DownloadOptions
from yaphoto_lib.options import media_scope as media_scope
from yaphoto_lib.options import scan_scope as scan_scope
from yaphoto_lib.options import validate_media as validate_media
from yaphoto_lib.util import atomic_json as atomic_json
from yaphoto_lib.util import auxiliary_path as auxiliary_path
from yaphoto_lib.util import canonical_path as canonical_path
from yaphoto_lib.util import progress_bar as progress_bar
from yaphoto_lib.util import safe_name as safe_name

if __name__ == "__main__":
    import sys

    sys.exit(main())
