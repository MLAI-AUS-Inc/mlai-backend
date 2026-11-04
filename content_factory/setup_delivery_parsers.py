"""Turn excessive JSON nesting into a bounded contract diagnostic."""

import logging
from rest_framework.exceptions import APIException
from rest_framework.parsers import JSONParser
from rest_framework.settings import api_settings

logger = logging.getLogger(__name__)


class DeliveryNestingError(APIException):
    """The JSON decoder cannot safely construct this input tree."""
    status_code = 422
    default_code = "delivery_schema_invalid"
    default_detail = {"code": "delivery_schema_invalid", "acknowledged": False,
                      "fieldErrors": [{"path": "/"}]}

    def __init__(self):
        super().__init__()
        # APIException normally stringifies every detail leaf. The delivery
        # protocol requires an actual boolean, including malformed JSON input.
        self.detail = {"code": "delivery_schema_invalid", "acknowledged": False,
                       "fieldErrors": [{"path": "/"}]}


class SetupDeliveryJSONParser(JSONParser):
    """Keep legacy parsing while preventing a recursive decoder crash."""
    def parse(self, stream, media_type=None, parser_context=None):
        try:
            return super().parse(stream, media_type=media_type, parser_context=parser_context)
        except RecursionError:
            logger.warning("setup_delivery_contract_rejected code=delivery_schema_invalid fields=['/']")
            raise DeliveryNestingError() from None


DELIVERY_PARSERS = [SetupDeliveryJSONParser,
    *(parser for parser in api_settings.DEFAULT_PARSER_CLASSES if parser is not JSONParser)]
