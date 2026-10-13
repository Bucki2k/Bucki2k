import unittest

from minero_btc.address import (
    AddressError,
    SecretDetectedError,
    address_to_script_pubkey,
    looks_like_secret,
    validate_payout_address,
)


class AddressTests(unittest.TestCase):
    def test_valid_addresses(self):
        cases = {
            # Dirección del bloque génesis (P2PKH).
            "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa": "76a91462e907b15cbf27d5425399ebf6f0fb50ebb88f1888ac",
            "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy": "a914b472a266d0bd89c13706a4132ccfb16f7c3b9fcb87",
            # Vectores de BIP173 (bech32) y BIP350 (bech32m).
            "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4": "0014751e76e8199196d454941c45d1b3a323f1433bd6",
            "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4": "0014751e76e8199196d454941c45d1b3a323f1433bd6",
            "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3":
                "00201863143c14c5166804bd19203356da136c985678cd4d27a1b8c6329604903262",
            "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0":
                "512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798",
        }
        for address, script in cases.items():
            with self.subTest(address=address):
                self.assertEqual(address_to_script_pubkey(address).hex(), script)

    def test_invalid_addresses(self):
        bad = [
            "",
            "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb",  # suma de verificación mal
            "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5",  # suma bech32 mal
            "bc1qW508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",  # mayúsculas mezcladas
            "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",  # testnet
            "mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn",  # testnet P2PKH
            # Taproot codificado con bech32 en vez de bech32m (vector inválido de BIP350).
            "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqh2y7hd",
            "bc1zw508d6qejxtdg4y5r3zarvaryvg6kdaj",  # v2 con bech32 (válido en BIP173, no en BIP350)
            # v2 bien codificada (BIP350) pero sin dueño hoy: la rechazamos para cobrar.
            "bc1zw508d6qejxtdg4y5r3zarvaryvaxxpcs",
            "hola",
            "0OIl",
        ]
        for address in bad:
            with self.subTest(address=address):
                with self.assertRaises(AddressError):
                    validate_payout_address(address)

    def test_secrets_are_refused(self):
        secrets = [
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
            "5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ",  # WIF de ejemplo
            "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi",
            "0c28fca386c7a227600b2fe50b7cae11ec86d3bf1fbe471be89827e19d72aa1d",
        ]
        for secret in secrets:
            with self.subTest(secret=secret[:10]):
                self.assertTrue(looks_like_secret(secret))
                with self.assertRaises(SecretDetectedError):
                    validate_payout_address(secret)

    def test_addresses_are_not_secrets(self):
        for address in ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
                        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"):
            self.assertFalse(looks_like_secret(address))

    def test_common_paste_formats(self):
        expected = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
        for pasted in ("bitcoin:bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4?amount=0.1&label=x",
                       "BITCOIN:BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4",
                       '"bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"',
                       "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4"):
            with self.subTest(pasted=pasted):
                self.assertEqual(validate_payout_address(pasted)[0], expected)

    def test_friendly_errors_for_other_networks(self):
        cases = {
            "lnbc2500u1pvjluezpp5qqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqypq": "Lightning",
            "satoshi@walletofsatoshi.com": "Lightning",
            "0x52908400098527886E0F7030069857D2E4169EE7": "Ethereum",
            "4" + "A" * 94: "Monero",
        }
        for text, word in cases.items():
            with self.subTest(text=text[:12]):
                with self.assertRaises(AddressError) as ctx:
                    validate_payout_address(text)
                self.assertIn(word, str(ctx.exception))

    def test_more_secret_formats_are_refused(self):
        words = ["abandon"] * 11 + ["about"]
        secrets = [
            ", ".join(words),
            " ".join(f"{i}. {w}" for i, w in enumerate(words, 1)),
            " ".join(words[:11]),  # una palabra perdida al copiar
            "0x0c28fca386c7a227600b2fe50b7cae11ec86d3bf1fbe471be89827e19d72aa1d",
            "0c28 fca3 86c7 a227 600b 2fe5 0b7c ae11 ec86 d3bf 1fbe 471b e898 27e1 9d72 aa1d",
            "KwdMAjGmerYanjeui5SHS7JkmpZvVipYvB2LJGU1ZxJwYvP98617",  # WIF comprimida
        ]
        for secret in secrets:
            with self.subTest(secret=secret[:15]):
                with self.assertRaises(SecretDetectedError):
                    validate_payout_address(secret)

    def test_links_with_labels_are_not_secrets(self):
        p2tr = "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0"
        p2wsh = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
        for pasted in (f"bitcoin:{p2tr}?message=Gracias,Jason;hola", f"bitcoin:{p2wsh}?label=Mi PC",
                       f"Bitcoin: {p2tr}", f"BITCOIN:{p2wsh.upper()}?label=Mi PC"):
            with self.subTest(pasted=pasted):
                self.assertIn(validate_payout_address(pasted)[0], (p2tr, p2wsh))
        with self.assertRaises(AddressError) as ctx:
            validate_payout_address(p2tr + ",")
        self.assertNotIsInstance(ctx.exception, SecretDetectedError)

    def test_mistyped_wif_is_still_a_secret(self):
        typo = "KwdMAjGmerYanjeui5SHS7JkmpZvVipYvB2LJGU1ZxJwYvP98618"
        with self.assertRaises(SecretDetectedError):
            validate_payout_address(typo)

    def test_single_word_gets_a_seed_hint(self):
        with self.assertRaises(AddressError) as ctx:
            validate_payout_address("abandon")
        self.assertIn("frase semilla", str(ctx.exception))

    def test_whitespace_is_trimmed(self):
        address, _ = validate_payout_address("  bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4\n")
        self.assertEqual(address, "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")


if __name__ == "__main__":
    unittest.main()
