import json
from typing import Any, Optional, Sequence, Union

from django.conf import settings

import safe_eth.eth.django.serializers as eth_serializers
from eth_typing import ChecksumAddress, HexStr
from hexbytes import HexBytes
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from safe_eth.eth import get_auto_ethereum_client
from safe_eth.eth.eip712 import eip712_encode_hash
from safe_eth.safe.safe_signature import SafeSignature, SafeSignatureType
from safe_eth.util.util import to_0x_hex_str

from safe_transaction_service.utils.serializers import get_safe_owners

from .models import SIGNATURE_LENGTH, SafeMessage, SafeMessageConfirmation
from .utils import get_hash_for_message, get_safe_message_hash_for_message


# Request serializers
class SafeMessageSignatureParserMixin:
    def get_valid_owner_from_signatures(
        self,
        safe_signatures: Sequence[SafeSignature],
        safe_address: ChecksumAddress,
        safe_message: Optional[SafeMessage],
    ) -> tuple[ChecksumAddress, SafeSignatureType]:
        """
        :param safe_signatures:
        :param safe_address:
        :param safe_message: Safe message database object (if already created)
        :return:
        :raises ValidationError:
        """
        if len(safe_signatures) != 1:
            raise ValidationError(
                f"1 owner signature was expected, {len(safe_signatures)} received"
            )

        ethereum_client = get_auto_ethereum_client()
        for safe_signature in safe_signatures:
            if not safe_signature.is_valid(ethereum_client, safe_address):
                raise ValidationError(
                    f"Signature={to_0x_hex_str(safe_signature.signature)} for owner={safe_signature.owner} is not valid"
                )

        owner = safe_signatures[0].owner
        signature_type = safe_signatures[0].signature_type
        if safe_message:
            # Check signature is not already in database
            if SafeMessageConfirmation.objects.filter(
                safe_message=safe_message, owner=owner
            ).exists():
                raise ValidationError(f"Signature for owner {owner} already exists")

        owners = get_safe_owners(safe_address)
        if owner in settings.BANNED_EOAS:
            raise ValidationError(
                f"Signer={owner} is not authorized to interact with the service"
            )

        if owner not in owners:
            raise ValidationError(f"{owner} is not an owner of the Safe")

        return owner, signature_type


class SafeMessageSerializer(SafeMessageSignatureParserMixin, serializers.Serializer):
    message = serializers.JSONField()
    safe_app_id = serializers.IntegerField(allow_null=True, default=None, min_value=0)
    signature = eth_serializers.HexadecimalField(
        min_length=65, max_length=SIGNATURE_LENGTH
    )
    origin = serializers.CharField(max_length=200, allow_null=True, default=None)

    def validate_origin(self, origin):
        # Origin field on db is a JsonField
        if origin:
            try:
                origin = json.loads(origin)
            except ValueError:
                pass
        else:
            origin = {}

        return origin

    def validate_message(self, value: Union[str, dict[str, Any]]):
        if isinstance(value, str):
            return value

        if isinstance(value, dict):
            try:
                eip712_encode_hash(value)
                return value
            except ValueError as exc:
                raise ValidationError(
                    f"Provided dictionary is not a valid EIP712 message {value}"
                ) from exc

        raise ValidationError(f"Provided value is not a valid message {value}")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        safe_address = self.context["safe_address"]
        message = attrs["message"]
        signature = attrs["signature"]
        attrs["safe"] = safe_address
        message_hash = get_hash_for_message(message)
        safe_message_hash = get_safe_message_hash_for_message(
            safe_address, message_hash
        )
        attrs["message_hash"] = safe_message_hash

        if SafeMessage.objects.filter(message_hash=safe_message_hash).exists():
            raise ValidationError(
                f"Message with hash {to_0x_hex_str(safe_message_hash)} for safe {safe_address} already exists in DB"
            )

        safe_signatures = SafeSignature.parse_signature(
            signature, safe_message_hash, safe_hash_preimage=message_hash
        )
        owner, signature_type = self.get_valid_owner_from_signatures(
            safe_signatures, safe_address, None
        )

        attrs["proposed_by"] = owner
        attrs["signature_type"] = signature_type.value
        return attrs

    def create(self, validated_data):
        signature = validated_data.pop("signature")
        signature_type = validated_data.pop("signature_type")

        safe_message = SafeMessage.objects.create(**validated_data)
        SafeMessageConfirmation.objects.create(
            safe_message=safe_message,
            owner=validated_data["proposed_by"],
            signature=signature,
            signature_type=signature_type,
        )
        return safe_message


class SafeMessageSignatureSerializer(
    SafeMessageSignatureParserMixin, serializers.Serializer
):
    signature = eth_serializers.HexadecimalField(
        min_length=65, max_length=SIGNATURE_LENGTH
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)

        safe_message: SafeMessage = self.context["safe_message"]
        attrs["safe_message"] = safe_message
        signature: HexStr = attrs["signature"]
        safe_address = safe_message.safe
        message_hash = get_hash_for_message(safe_message.message)
        safe_message_hash = safe_message.message_hash

        safe_signatures = SafeSignature.parse_signature(
            signature, safe_message_hash, safe_hash_preimage=message_hash
        )
        owner, signature_type = self.get_valid_owner_from_signatures(
            safe_signatures, safe_address, safe_message
        )

        attrs["owner"] = owner
        attrs["signature_type"] = signature_type.value
        return attrs

    def create(self, validated_data):
        safe_message_confirmation = SafeMessageConfirmation.objects.create(
            **validated_data
        )
        safe_message = validated_data["safe_message"]
        SafeMessage.objects.filter(pk=safe_message.pk).update(
            modified=safe_message_confirmation.modified
        )
        return safe_message_confirmation


# Response serializers
class SafeMessageConfirmationResponseSerializer(serializers.Serializer):
    created = serializers.DateTimeField()
    modified = serializers.DateTimeField()
    owner = eth_serializers.EthereumAddressField()
    signature = eth_serializers.HexadecimalField()
    signature_type = serializers.SerializerMethodField()

    def get_signature_type(self, obj: SafeMessageConfirmation) -> str:
        return SafeSignatureType(obj.signature_type).name


class SafeMessageResponseSerializer(serializers.Serializer):
    created = serializers.DateTimeField()
    modified = serializers.DateTimeField()
    safe = eth_serializers.EthereumAddressField()
    message_hash = eth_serializers.Sha3HashField()
    message = serializers.JSONField()
    proposed_by = eth_serializers.EthereumAddressField()
    safe_app_id = serializers.IntegerField()
    confirmations = serializers.SerializerMethodField()
    prepared_signature = serializers.SerializerMethodField()
    origin = serializers.SerializerMethodField()

    def get_confirmations(self, obj: SafeMessage) -> dict[str, Any]:
        """
        Filters confirmations queryset

        :param obj: SafeMessage instance
        :return: Serialized queryset
        """
        return SafeMessageConfirmationResponseSerializer(
            obj.confirmations, many=True
        ).data

    def get_prepared_signature(self, obj: SafeMessage) -> Optional[str]:
        """
        Prepared signature sorted

        :param obj: SafeMessage instance
        :return: Serialized queryset
        """
        signature = HexBytes(obj.build_signature())
        return to_0x_hex_str(HexBytes(signature)) if signature else None

    def get_origin(self, obj: SafeMessage) -> str:
        return obj.origin if isinstance(obj.origin, str) else json.dumps(obj.origin)
