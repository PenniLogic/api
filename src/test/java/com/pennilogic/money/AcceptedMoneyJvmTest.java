package com.pennilogic.money;

import com.pennilogic.contracts.money.CurrencyEntry;
import com.pennilogic.contracts.money.CurrencyRegistry;
import com.pennilogic.contracts.money.Money;
import com.pennilogic.contracts.money.MoneyReason;
import com.pennilogic.contracts.money.MoneySerializer;
import com.pennilogic.contracts.money.MoneyWire;
import com.pennilogic.contracts.money.MoneyWireException;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Objects;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class AcceptedMoneyJvmTest {
    private static void same(Object expected, Object actual, String operation) {
        assertTrue(Objects.equals(expected, actual), operation);
    }

    // Exercise the existing JVM-visible internal seams, never private constructors or altered provider state.
    private static Object internal(Object receiver, String operation, Object... arguments) throws Throwable {
        List<Method> matches = Arrays.stream(receiver.getClass().getMethods())
            .filter(method -> method.getName().startsWith(operation + "$"))
            .toList();
        same(1, matches.size(), "unique-provider-seam");
        try {
            return matches.getFirst().invoke(receiver, arguments);
        } catch (InvocationTargetException error) {
            throw error.getCause();
        }
    }

    MoneyWire wire(long input, String code) throws Throwable {
        return (MoneyWire) internal(Money.Companion.ofMinorUnits(input, code), "toWire");
    }

    @Test
    void registryValueOperationsPreserveEveryFieldAndOriginalEntry() {
        for (CurrencyEntry entry : CurrencyRegistry.INSTANCE.getEntries().values()) {
            same(entry.getCode(), entry.component1(), "registry-code-component");
            same(entry.getExponent(), entry.component2(), "registry-exponent-component");
            same(entry.getMinorUnitName(), entry.component3(), "registry-unit-component");
            CurrencyEntry copy = entry.copy(entry.getCode(), entry.getExponent(), entry.getMinorUnitName());
            assertFalse(copy == entry, "registry-copy-is-independent");
            assertTrue(entry.equals(entry), "registry-reflexivity");
            assertTrue(entry.equals(copy) && copy.equals(entry), "registry-copy-equality");
            assertFalse(entry.equals(null), "registry-null-equality");
            assertFalse(entry.equals(entry.getCode()), "registry-other-type-equality");
            same(entry.hashCode(), copy.hashCode(), "registry-equal-hashes");
            same(
                "CurrencyEntry(code=" + entry.getCode() + ", exponent=" + entry.getExponent()
                    + ", minorUnitName=" + entry.getMinorUnitName() + ")",
                entry.toString(), "registry-diagnostic-fields"
            );
            for (CurrencyEntry other : CurrencyRegistry.INSTANCE.getEntries().values()) {
                same(
                    other, entry.copy(other.getCode(), other.getExponent(), other.getMinorUnitName()),
                    "registry-explicit-copy-fields"
                );
                same(
                    entry.getCode().equals(other.getCode()), entry.equals(other),
                    "registry-distinct-entry-equality"
                );
                if (!entry.equals(other)) {
                    assertFalse(entry.equals(entry.copy(other.getCode(), entry.getExponent(), entry.getMinorUnitName())),
                        "registry-code-participates-in-equality");
                    assertFalse(entry.equals(entry.copy(entry.getCode(), other.getExponent(), entry.getMinorUnitName())),
                        "registry-exponent-participates-in-equality");
                    assertFalse(entry.equals(entry.copy(entry.getCode(), entry.getExponent(), other.getMinorUnitName())),
                        "registry-unit-participates-in-equality");
                }
            }
            same(entry, CurrencyRegistry.INSTANCE.getEntries().get(entry.getCode()), "registry-original-unchanged");
        }
    }

    @Test
    void rawCarrierValueOperationsPreserveBothFieldsWithoutAdmittingAnotherWireType() throws Throwable {
        for (String code : CurrencyRegistry.INSTANCE.getEntries().keySet()) {
            MoneyWire first = wire(1, code);
            MoneyWire other = wire(2, code);
            MoneyWire copy = first.copy(first.getAmount(), first.getCurrency());
            same(first.getAmount(), first.component1(), "carrier-first-component");
            same(first.getCurrency(), first.component2(), "carrier-second-component");
            assertFalse(first == copy, "carrier-copy-is-independent");
            assertTrue(first.equals(first), "carrier-reflexivity");
            assertTrue(first.equals(copy) && copy.equals(first), "carrier-copy-equality");
            assertFalse(first.equals(null), "carrier-null-equality");
            assertFalse(first.equals(code), "carrier-other-type-equality");
            assertFalse(first.equals(other), "carrier-distinct-value-equality");
            same(first.hashCode(), copy.hashCode(), "carrier-equal-hashes");
            same(other, first.copy(other.getAmount(), other.getCurrency()), "carrier-explicit-copy-fields");
            same(
                "MoneyWire(amount=" + first.getAmount() + ", currency=" + code + ")",
                first.toString(), "carrier-diagnostic-fields"
            );
            for (String otherCode : CurrencyRegistry.INSTANCE.getEntries().keySet()) {
                if (!otherCode.equals(code)) {
                    // A raw carrier copy is not Money and is never submitted to the public serializer.
                    assertFalse(first.equals(first.copy(first.getAmount(), otherCode)), "carrier-currency-equality");
                }
            }
            same(first, wire(1, code), "carrier-original-unchanged");
        }
    }

    @Test
    void valueCollectionsRespectProviderEquality() throws Throwable {
        var values = new HashSet<Money>();
        var carriers = new HashSet<MoneyWire>();
        var entries = new HashMap<CurrencyEntry, String>();
        for (String code : CurrencyRegistry.INSTANCE.getEntries().keySet()) {
            for (long input : new long[] {-Long.MAX_VALUE, -1, 0, 1, Long.MAX_VALUE}) {
                Money value = Money.Companion.ofMinorUnits(input, code);
                assertTrue(values.add(value), "distinct-money-key");
                assertFalse(values.add(Money.Companion.ofMinorUnits(input, code)), "equal-money-key");
                assertTrue(carriers.add(wire(input, code)), "distinct-carrier-key");
                assertFalse(carriers.add(wire(input, code)), "equal-carrier-key");
            }
            CurrencyEntry entry = CurrencyRegistry.INSTANCE.getEntries().get(code);
            entries.put(entry, code);
            same(code, entries.get(entry.copy(entry.getCode(), entry.getExponent(), entry.getMinorUnitName())),
                "equal-registry-map-key");
        }
        same(15, values.size(), "money-set-cardinality");
        same(15, carriers.size(), "carrier-set-cardinality");
        same(CurrencyRegistry.INSTANCE.getEntries().size(), entries.size(), "registry-map-cardinality");
    }

    @Test
    void collisionHeavyValueCollectionsStillRespectCurrencies() {
        var values = new HashSet<Money>();
        int cases = 0;
        for (String code : CurrencyRegistry.INSTANCE.getEntries().keySet()) {
            for (int index = 1; index <= 128; index++) {
                // Distinct valid inputs with equal Long hashes exercise the collection's collision path.
                long input = ((long) index << 32) | index;
                Money first = Money.Companion.ofMinorUnits(input, code);
                assertTrue(values.add(first), "collision-distinct-key");
                assertTrue(values.contains(Money.Companion.ofMinorUnits(input, code)), "collision-equal-key");
                cases++;
            }
        }
        same(cases, values.size(), "collision-key-cardinality");
        System.out.println("{\"event\":\"money_property_cases\",\"property\":\"collision_keys\",\"count\":" + cases + "}");
    }

    @Test
    void rejectionDiagnosticsHaveExactlyTheDeclaredReasonAndFieldAndNoValue() {
        for (MoneyReason reason : MoneyReason.getEntries()) {
            for (String field : new String[] {"", "amount", "currency"}) {
                MoneyWireException error = new MoneyWireException(reason, field);
                same(reason, error.getReason(), "rejection-reason");
                same(field, error.getField(), "rejection-field");
                same(
                    "money rejected: " + reason.getWireName() + " at " + (field.isEmpty() ? "<value>" : field),
                    error.getMessage(), "rejection-minimal-diagnostic"
                );
            }
        }
        Money left = Money.Companion.ofMinorUnits(1, "INR");
        Money right = Money.Companion.ofMinorUnits(1, "JPY");
        for (var operation : List.<org.junit.jupiter.api.function.Executable>of(
            () -> left.plus(right), () -> left.minus(right), () -> left.compareTo(right)
        )) {
            same("Money operates only within one currency",
                assertThrows(IllegalArgumentException.class, operation).getMessage(), "mixed-currency-diagnostic");
        }
    }

    @Test
    void nullableJavaCallersCannotBypassDeclaredNonNullProviderBoundaries() throws Throwable {
        Money value = Money.Companion.ofMinorUnits(1, "INR");
        MoneyWire carrier = wire(1, "INR");
        CurrencyEntry entry = CurrencyRegistry.INSTANCE.getEntries().get("INR");
        for (var operation : List.<org.junit.jupiter.api.function.Executable>of(
            () -> Money.Companion.ofMinorUnits(1, null),
            () -> Money.Companion.parse(null, "INR"),
            () -> Money.Companion.parse("0.00", null),
            () -> Money.Companion.fromWire(null),
            () -> value.plus(null), () -> value.minus(null), () -> value.compareTo(null),
            () -> CurrencyRegistry.INSTANCE.exponentOf(null),
            () -> new CurrencyEntry(null, entry.getExponent(), entry.getMinorUnitName()),
            () -> new CurrencyEntry(entry.getCode(), entry.getExponent(), null),
            () -> entry.copy(null, entry.getExponent(), entry.getMinorUnitName()),
            () -> entry.copy(entry.getCode(), entry.getExponent(), null),
            () -> new MoneyWire(null, carrier.getCurrency()),
            () -> new MoneyWire(carrier.getAmount(), null),
            () -> carrier.copy(null, carrier.getCurrency()), () -> carrier.copy(carrier.getAmount(), null),
            () -> new MoneyWireException(null, "amount"), () -> new MoneyWireException(MoneyReason.SHAPE, null),
            () -> MoneySerializer.INSTANCE.deserialize(null),
            () -> MoneySerializer.INSTANCE.serialize(null, value)
        )) {
            assertThrows(NullPointerException.class, operation, "non-null-jvm-boundary");
        }
    }

    @Test
    void theInternalParserStillValidatesGrammarBeforeScaleAndRange() throws Throwable {
        for (String text : new String[] {"", "synthetic-malformed", "-0.00", "+1.00", "01.00", "1."}) {
            MoneyWireException error = assertThrows(MoneyWireException.class,
                () -> internal(Money.Companion, "minorUnitsFromCanonical", text, 2), "internal-grammar");
            same(MoneyReason.GRAMMAR, error.getReason(), "internal-grammar-precedence");
            same("amount", error.getField(), "internal-grammar-field");
        }
        same(0L, internal(Money.Companion, "minorUnitsFromCanonical", "0", 0), "internal-zero-integral");
        same(0L, internal(Money.Companion, "minorUnitsFromCanonical", "0.00", 2), "internal-zero-scaled");
        same(-1L, internal(Money.Companion, "minorUnitsFromCanonical", "-0.001", 3), "internal-negative-scaled");
    }
}
