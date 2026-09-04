"""Provider outcomes that must not be converted into retryable failures."""


class AiProviderResultUnknown(RuntimeError):
    """A provider may have accepted work, but no terminal result is proven."""
