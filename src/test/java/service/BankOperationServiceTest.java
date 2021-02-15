package service;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class BankOperationServiceTest extends ServiceTestInit {


    @Test
    @DisplayName("deposit - should make a deposit and update client's account balance")
    public void shouldMakeADeposit() {
        // GIVEN
        double preOperationBalance = client.getAccount().getBalance();
        double operationAmount = 100.12d;

        // WHEN
        BankOperationService.deposit(client.getAccount(), operationAmount);

        // THEN
        assertEquals(preOperationBalance + operationAmount, client.getAccount().getBalance());
        assertEquals(1, client.getAccount().getOperations().size());
    }


    @Test
    @DisplayName("withdrawal - should make a withdrawal and update client's account balance")
    public void shouldMakeAWithdrawal() {
        // GIVEN
        double preOperationBalance = client.getAccount().getBalance();
        double operationAmount = 100.12d;

        // WHEN
        BankOperationService.withdrawal(client.getAccount(), operationAmount);

        // THEN
        assertEquals(preOperationBalance - operationAmount, client.getAccount().getBalance());
        assertEquals(1, client.getAccount().getOperations().size());
    }


    @Test
    @DisplayName("history - should add multiple operation to the account's history")
    public void checkHistoryShouldReturnAStringWithAllPastOperationsInfo() {
        // GIVEN
        double preOperationBalance = client.getAccount().getBalance();
        double operationAmount = 100.12d;

        // WHEN
        BankOperationService.withdrawal(client.getAccount(), operationAmount);
        BankOperationService.deposit(client.getAccount(), operationAmount);
        BankOperationService.deposit(client.getAccount(), operationAmount);
        String historyOutput = BankOperationService.checkHistory(client.getAccount());

        // THEN
        assertEquals(preOperationBalance + operationAmount, client.getAccount().getBalance());
        assertEquals(3, client.getAccount().getOperations().size());
        assertTrue(historyOutput.contains("balance=1899.88"));
        assertTrue(historyOutput.contains("balance=1899.88"));
        assertTrue(historyOutput.contains("balance=2000.0"));
        assertTrue(historyOutput.contains("operationType=DEPOSIT, amount=100.12"));
        assertTrue(historyOutput.contains("operationType=WITHDRAWAL, amount=100.12"));

    }
}