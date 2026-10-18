"""Workspace layout: every workspace-relative path in one table (POSIX separators)."""

CONFIG = 'config.json'
POLICY = 'config/policy.json'
PLATFORMS = 'config/platforms.json'
BROWSER = 'config/browser.json'

PROFILE_DIR = 'profile'
IDENTITY = 'profile/identity.json'
PROFILE = 'profile/PROFILE.md'
EXPERIENCE = 'profile/EXPERIENCE.md'
STORY_BANK = 'profile/STORY-BANK.md'
VOICE = 'profile/VOICE.md'
HR = 'profile/HR-DETAILS.json'
FORM_ANSWERS = 'profile/FORM-ANSWERS.json'
MARKET_JSON = 'profile/market.json'
MARKET_MD = 'profile/MARKET.md'
SIGNATURE = 'profile/signature.html'
SIGNATURE_META = 'profile/signature.json'
SOURCES = 'profile/sources'
SOURCES_INDEX = 'profile/sources/index.json'

CV_DIR = 'cv'
CV_MASTER = 'cv/master-cv.tex'
CV_MANIFEST = 'cv/cv-manifest.json'
CV_BASELINE = 'cv/baseline.pdf'
CV_VARIANTS = 'cv/variants'
CV_BUILDS = 'cv/builds'
CV_REVIEWS = 'cv/reviews'

LEARNING = 'learning'
CHANGES = 'learning/changes.jsonl'
CHANGELOG = 'learning/CHANGELOG.md'
LEARNED = 'learning/LEARNED.md'
INBOX = 'learning/inbox.jsonl'
INDEX = 'learning/index.json'
TRANSACTIONS = 'learning/transactions'
LOG_FRAGMENTS = 'learning/log-fragments'
DIFFS = 'learning/diffs'

JOBS = 'jobs'
REGISTRY = 'jobs/registry.json'
OUTREACH = 'jobs/outreach.json'
JOB_HISTORY = 'jobs/history'
JOB_SOURCES = 'jobs/sources'
PIPELINE = 'jobs/pipeline'
PIPELINE_STATE = 'jobs/pipeline/state.json'
APPLICATIONS = 'jobs/applications'
DRAFTS = 'jobs/drafts'
OUTPUTS = 'jobs/outputs'
TRACKER = 'jobs/outputs/Tracker.xlsx'
CV_OUTPUTS = 'jobs/outputs/cv'

STATE = '.state'
ONBOARDING = '.state/onboarding.json'
CONNECTIONS = '.state/connections.json'
CONNECT_LOG = '.state/connect-log.jsonl'
APPROVALS = '.state/approvals'
LOCKS = '.state/locks'
ROOT_LOCK = '.state/locks/workspace.lock'
SANDBOX = '.state/sandbox'
RENDERS = '.state/renders'
REVISION_INDEX = '.state/revision-index.json'
TRACKER_STATE = '.state/tracker'
PROPOSALS = '.state/proposals'

# Directories created for a new workspace.
SKELETON = (PROFILE_DIR, SOURCES, CV_DIR, CV_VARIANTS, LEARNING, JOBS, APPLICATIONS, OUTPUTS, 'config', STATE, LOCKS)


def sandboxed(relative, test=False):
    """Fictional/evaluation storage lives under .state/sandbox, never beside real records."""
    return SANDBOX + '/' + relative if test else relative
