class BenthicMCPError(Exception):
    pass


class CatalogError(BenthicMCPError):
    pass


class QueryValidationError(BenthicMCPError):
    pass


class UpstreamError(BenthicMCPError):
    pass
