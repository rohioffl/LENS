from .aws_inventory import *  # noqa

# Explicitly import task modules so Django app registry imports succeed.
from . import aws_task  # noqa: F401
from . import terraform_task  # noqa: F401
from . import classic_vpn_task  # noqa: F401
from . import ecr_migration_task  # noqa: F401
from . import ha_vpn_task  # noqa: F401
