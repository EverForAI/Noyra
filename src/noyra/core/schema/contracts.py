"""Contracts definitions; execution remains in core.database."""

from __future__ import annotations

# Schema versions describe the complete SQLite contract. Optional runtime
# features may still be repaired idempotently, but they must not be invisible
# to migration/export consumers.

# The schema DDL fingerprint is checked after every successful initialization.
# Update this value only alongside a reviewed schema migration and its tests.
# This is the complete contract, including security triggers.  Existing v77
# installations persist this value in ``schema_meta`` and therefore require
# the value to remain stable across compatible runtime fixes.

# Structural objects are checked independently so a trigger-only integrity
# defect can reach the startup integrity gate and be quarantined with an
# auditable P0 finding.  Tables, indexes, and views still fail closed during
# database construction because they define the storage contract itself.


_SCHEMA_DDL_FINGERPRINTS: dict[int, str] = {
    108: "1da2cb3d1e370734e052decb4c951cf4c0105f847ed46d1daeee9e91de59ffa6",
    107: "927d782afd08592da1313d8ad17c1c7f3110127564328541ae94f2d3252ff31c",
    105: "f117dc793fddf96e769abef73c30127b5494ce8fb8b7f6598e6b1e093f554950",
    106: "236bf850b21704b51478101c6a979682307f69f4530f937287eaec46c14870c5",
    104: "72e5c15e4793cbe3b743b5eca667cea5bef3c34af05a4674bde7ce720234dc8e",
    103: "b13fec71f4c1a40287f7b7b272f7ba8e12bdd9625aa53ca08568023c25f84334",
    102: "f731a5afa55d7e6c7adf170f93e985e0b1efee544f365dfd746f31afc997db09",
    101: "f96066b2cb8abf958820d6340dfc1d00e60dcd491db84caef6f09f0e458c3301",
    100: "e056dd1ba603c0d6f44444b24c668e4008ed3701c36155dbb5e742e328f7616a",
    99: "289738612874ddce653fcc53979ab3285f0601920cde5aad27baafee978d1145",
    98: "8a16f2c86d53e37f52f96154d49dda2630a22b411650f62f33fdaa05d230c415",
    97: "39d19922cfd11f01d60b4d2085f50fdc8eb0fa6b1a213d0594aaa5e39d46ad36",
    96: "f851774364cf13313d4a670104d5098ed362cb4c1e4422b76c7efd68c2e6e9dc",
    95: "4d897bceb70f95ea1310b2a637d0606ff26dc70a8ff73e0f71094ce98e95fca5",
    94: "181b72baa819d54d6d18c80411b610abdd2c3c6cee18661aeadb3a190a339f9b",
    93: "8968045531bd80f2f1d487cb9a4b01e2b84767b10b8a85fc4ce85d0c956022bb",
    92: "28e3601ed49497ace746afc16374512e35491c6a3cd30299d4fd8122eab4c543",
    91: "7747814bce9f944f0b64f407da16524a7728671272797273eefc5ba72193daab",
    90: "649a64f023b29e3dddc355e0e45598e77446876884f3adeb0c199b1677ba8eb5",
    89: "dd50e35a4449d3c4dbe424ec7ed4ccb7464665a01efbb5b30131a5578f4abc55",
    88: "0a0e187d52eadc7cf9631117a9a864ceb0882e3ead3a4461306d80d759813119",
    87: "9f7249ee4e7d465438cc06aa0d9a635a281a0a3118ae1210407c14c242d8ee2f",
    86: "43368f8885f52e6fc915a25f60ad983dd6f55a0de28007f18b6603f4cf55fbd3",
    85: "1866b81f3fbe8104021f130564d175b61961100a4524c84d3ef3a7ffd1c012e4",
    84: "a3a27655b433d22d0b2148267592067c724e17f37622fa95c1c69b0bd88496a4",
    83: "ad714681e3e85d60a20ab14ebabc4106285df457bfd8fc633c002097b12c2505",
    82: "5a6031407e2cc1e55da5706c5ab8d0fe749286b8402976eae76fc1f8c71d4288",
    81: "4d82e1926e29d7fd6d01e59149017eab575f50fff949f0827b3dab3918d1beb1",
    80: "70e0fcbd9ee1929f64bd9869d7070496ff45b0f14dee7d0bb9db316e62d12326",
    77: "2487f1a2703fc940178b2c77f2b3982a37b4a526de37b9b253204be71f4a6d1f",
    78: "895ea957c9befde841e2c665c1cae4d47d9f90c9b37a9f6341e2cea339bbd5d5",
    79: "af5da5087bbb8cac85b14699d10fa2e706d07b2eb225e3a22b19ed87ea08e7c6",
}


_SCHEMA_STRUCTURE_FINGERPRINTS: dict[int, str] = {
    108: "12c03b4c9b9f39913771c37913e8df12dec9e4d7971829c8db4f41e20d206704",
    107: "6f8c0acae3783f1c6053c14161e5203c1369371e9c95d8bb3630a5211426725f",
    105: "8feff8b35da5fad5d7c530dd492dcf78347c038456f48857c4afad63fd220a2e",
    106: "60c959c1499b63793106847aae7da21ad44733ecc143373b44e5ea2313450241",
    104: "1c17714c61ce55ec2a36af8dfdb439bbae32428bab58c7d842a34802426650e4",
    103: "1147ca9c2c26d160b0b20186536e1bf4a4f34211aab668a25af36610bdaa284b",
    102: "cb7f1359673e8949c90b1706e14de28c857d8df4b54c07aa0e4f136bf3aa4900",
    101: "8ef1b897245fdf0ae876e0f9a03ca6df381bfb0412ca804d202708cfdc22bd92",
    100: "47b11a9ef31e674e8d9610620d7b6acaf9e33aa263f1383c095edc9411a965a8",
    99: "f6d0ed82b9a6fc8ead5f18dac96c904f04f5087376a2ec9ed1553d55d41dc2fd",
    98: "1c71052010dca131f02eeb04c66f9c38422be66a3374d31d3a39c4e4ec1f20b0",
    97: "2f1a1176f4b0882132078c303add11e9b525d9901b9f421023f9c78d1f45c7fc",
    96: "96413e10b9c7d69bf591d15fc84be46a942aff186c6b40a923ee968243dcabf6",
    95: "58c35537c7ed5fd43b871c53d925ba4f8cdc3dcefdb2d65a72172c89f4ba29c0",
    94: "762feef35a9e0593168f010ea4f980d3425bf55db788baf71bc587a9c9333e2e",
    93: "bd4f12a88b407fc6f3fe4ab713401afb9c1a4ba73d79146927898a2ca8211b36",
    92: "09ccb3fabc0eb74acae5bb8b3a6fa540d1fda3a436f1b62ab32755e9d46226a6",
    91: "ab84d8a689063a3e2313c587f3dc7bd20795bac4187f00f2fade22b794de43c4",
    90: "96618523ee767b8249c9c03775ff330c28195e68579a7e344475a369cf29400d",
    89: "fddaa5e147c496247e6a1c895303a77b52d3d64a8e1fe22cb060a5b18a81b3b0",
    88: "67486314faded669b7b3d6bcdcd619f90a7abdf22da3f01ec07127abe896cfc5",
    87: "67486314faded669b7b3d6bcdcd619f90a7abdf22da3f01ec07127abe896cfc5",
    86: "d54e6742307c5ec757afc98430368d0e11c3dfe0b87d1a3372f141dc9f2c292f",
    85: "57ef8566669d777860b4c97c3cf4f58e2050d3ca40bae05972094d564e7501a6",
    84: "60389656a9820b16ef4321937a04c139e068c4416189544938017ab586d017b5",
    83: "a9e34bb9c15d3499f211d315fde416314012a9754fee7fe535e87f59d4ff731f",
    82: "c7071ba23a95d0c56f7518ceb6cdd9c2609a58fb9978cd1deb6bc84d04292457",
    81: "34d4cbb66c5433a6cb4614cba22b2a5dd69b55705a56733b1af38b26da297843",
    80: "840bae4d215def4cb6425ac5977c2a093fdaf90cfd7f2b5ec841c27ba0afd9dd",
    77: "03eae2bf8cdb2379f5a7271801044ce1920e0805018827d3661184b2a9fca896",
    78: "d8cc66309f41f32f3c7a7f05954ac237ad3826cfcdf6cf178c07538aa30fe828",
    79: "d7b6e06f229327411a7862d6b65fd452a3cd88ab3f1034f6e3002d9f4d4e32d4",
}


_PERSISTENT_FEATURE_OBJECTS: dict[str, tuple[str, ...]] = {
    "world_poll_state": (
        "world_poll_state",
        "idx_world_poll_subject",
        "world_feed_seen",
        "idx_world_feed_seen_age",
        "validate_world_poll_subject_insert",
        "validate_world_poll_subject_update",
        "validate_world_feed_seen_subject",
    ),
    "secret_cleanup": ("secret_cleanup_queue", "idx_secret_cleanup_subject_status"),
    "provider_health_metrics": ("provider_health_buckets",),
    "secret_file_intents": (
        "secret_file_intents",
        "idx_secret_file_intents_subject_state",
        "validate_secret_file_intent_reference_binding",
        "validate_secret_file_intent_intent_binding",
        "validate_secret_file_intent_reference_binding_update",
        "validate_secret_file_intent_identity_immutable",
        "prevent_secret_file_intent_delete",
        "validate_secret_file_intent_transition",
        "validate_secret_file_intent_operation_state",
    ),
    "search_provider_routing": (
        "search_provider_routing",
        "idx_search_provider_routing_order",
    ),
    "model_capability_profiles": (
        "model_capability_profiles",
        "idx_model_capability_profiles_subject",
    ),
}


_PERSISTENT_FEATURE_MIN_SCHEMA: dict[str, int] = {
    "world_poll_state": 108,
    "provider_health_metrics": 68,
    "search_provider_routing": 77,
    "model_capability_profiles": 99,
}


def _canonical_schema_sql(source: str) -> str:
    """Normalize harmless DDL formatting while preserving quoted values."""
    tokens: list[str] = []
    punctuation = set("(),.;=<>+-*/%|&~!^:?{}").union({"[", "]"})
    index = 0
    while index < len(source):
        character = source[index]
        if character.isspace():
            index += 1
            continue
        if source.startswith("--", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            comment_end = source.find("*/", index + 2)
            index = len(source) if comment_end < 0 else comment_end + 2
            continue
        if character in "'\"`[":
            start = index
            terminator = "]" if character == "[" else character
            index += 1
            while index < len(source):
                if source[index] == terminator:
                    if (
                        terminator != "]"
                        and index + 1 < len(source)
                        and source[index + 1] == terminator
                    ):
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            tokens.append(source[start:index])
            continue
        if character in punctuation:
            tokens.append(character)
            index += 1
            continue
        start = index
        while index < len(source):
            character = source[index]
            if (
                character.isspace()
                or character in punctuation
                or character in "'\"`["
                or source.startswith("--", index)
                or source.startswith("/*", index)
            ):
                break
            index += 1
        tokens.append(source[start:index])
    return "\x1f".join(tokens)
