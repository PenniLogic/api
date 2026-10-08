package com.pennilogic.testing

import com.pennilogic.contracts.money.CurrencyRegistry
import com.pennilogic.contracts.money.Money
import com.pennilogic.contracts.money.MoneySerializer
import com.pennilogic.contracts.money.MoneyWireException
import kotlinx.serialization.json.Json
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Tag
import org.junit.jupiter.api.Test

@Tag("property")
class MoneyWirePropertyTest {
    @Test
    fun `shared values round trip through the accepted codec and labeled invalid values refuse`() {
        checkProperty("money-wire", 256, SharedGenerators.values()) { case ->
            when (val input = case.input) {
                is ValueInput.Accepted -> {
                    val first: Money = input.value
                    val encoded = Json.encodeToString(MoneySerializer, first)
                    val decoded = Json.decodeFromString(MoneySerializer, encoded)
                    case.verify("wire-value", first == decoded)
                    case.verify("wire-canonical-bytes", encoded == Json.encodeToString(MoneySerializer, decoded))
                    for (currency in CurrencyRegistry.entries.keys.filter { it != first.currency }) {
                        val other: Money = Money.ofMinorUnits(first.minorUnits, currency)
                        val otherWire = Json.encodeToString(MoneySerializer, other)
                        case.verify("other-wire-value", other == Json.decodeFromString(MoneySerializer, otherWire))
                        assertThrows(IllegalArgumentException::class.java) { first + other }
                        assertThrows(IllegalArgumentException::class.java) { first.compareTo(other) }
                    }
                }

                is ValueInput.Refused -> {
                    val error = assertThrows(MoneyWireException::class.java) { Money.ofMinorUnits(input.units, input.currency) }
                    case.verify("refusal-reason", input.reason == error.reason)
                }
            }
        }
    }
}
