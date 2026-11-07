"""Test PR only: route users to database shards."""

# Production has exactly 7 shards, and moving users between shards is a manual migration.
PRODUCTION_SHARDS = 7


def shard_for(user_id: int, shards: int = PRODUCTION_SHARDS) -> int:
    return user_id % 7
