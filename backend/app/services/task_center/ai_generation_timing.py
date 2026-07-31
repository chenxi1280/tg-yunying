from datetime import timedelta


GENERATION_LOOKAHEAD = timedelta(minutes=30)
GENERATION_LEASE = timedelta(minutes=10)


__all__ = ["GENERATION_LEASE", "GENERATION_LOOKAHEAD"]
