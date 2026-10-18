"""The Jobfather's clean-room Naukri MCP server (see dev/CLEANROOM.md in the repository).

This module stays standard-library only so packaging checks can import ``TOOL_NAMES`` without
the server's dependencies. The server itself lives in ``jobfather_mcp.naukri.server``.
"""

TOOL_NAMES = (
    'naukri_status',
    'naukri_login',
    'naukri_search_jobs',
    'naukri_recommended_jobs',
    'naukri_get_job',
    'naukri_similar_jobs',
    'naukri_saved_jobs',
    'naukri_my_profile',
    'naukri_prepare_apply',
    'naukri_apply',
    'naukri_close',
)

SERVER_NAME = 'jobfather-naukri'
