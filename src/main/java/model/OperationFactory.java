package model;


import java.time.LocalDateTime;


/**
 * The use of a factory is an architectural choice. This goes along with the solid principles. But maybe not with KISS logic
 */
public class OperationFactory {

    public BankOperation getBankOperation(OperationType operationType, double amount, double balance) {
        return switch (operationType) { // Using java 14 switch case syntax
            case DEPOSIT -> new DepositBankOperation(OperationType.DEPOSIT, amount, LocalDateTime.now(), balance);
            case WITHDRAWAL -> new WithdrawalBankOperation(OperationType.WITHDRAWAL, amount, LocalDateTime.now(), balance);
        };

    }
}
