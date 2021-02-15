package service;

import exception.AccountBlockedException;
import exception.InvalidOperationAmountException;
import exception.MinBalanceExceededException;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertThrows;

class BankOperationValidatorTest extends ServiceTestInit {


    @Test
    @DisplayName("depositValidation - should throw an AccountBlockedException when the account is blocked")
    public void shouldThrowAccountBlockedExceptionWhenCallingDepositValidation() {
        // GIVEN
        client.getAccount().setBlocked(true);
        // WHEN - THEN
        assertThrows(AccountBlockedException.class, () -> BankOperationValidator.depositValidation(client.getAccount(), 10d));
    }


    @Test
    @DisplayName("depositValidation - should throw an InvalidOperationAmountException when the amount is <0")
    public void shouldThrowInvalidOperationAmountExceptionWhenCallingDepositValidation() {
        // GIVEN - WHEN - THEN
        assertThrows(InvalidOperationAmountException.class, () -> BankOperationValidator.depositValidation(client.getAccount(), -10d));
    }


    @Test
    @DisplayName("withdrawalValidation - should throw an AccountBlockedException when the account is blocked")
    public void shouldThrowAccountBlockedExceptionWhenCallingWithdrawalValidation() {
        // GIVEN
        client.getAccount().setBlocked(true);
        // WHEN - THEN
        assertThrows(AccountBlockedException.class, () -> BankOperationValidator.withdrawalValidation(client.getAccount(), 10d));
    }


    @Test
    @DisplayName("withdrawalValidation - should throw an InvalidOperationAmountException when the amount is <0")
    public void shouldThrowInvalidOperationAmountExceptionWhenCallingWithdrawalValidation() {
        // GIVEN - WHEN - THEN
        assertThrows(InvalidOperationAmountException.class, () -> BankOperationValidator.withdrawalValidation(client.getAccount(), -10d));
    }


    @Test
    @DisplayName("withdrawalValidation - should throw an MinBalanceExceededException when the min balance is exceeded")
    public void shouldThrowMinBalanceExceededExceptionWhenCallingWithdrawalValidation() {
        // GIVEN - WHEN - THEN
        assertThrows(MinBalanceExceededException.class, () -> BankOperationValidator.withdrawalValidation(client.getAccount(), 5000.32));
    }
}