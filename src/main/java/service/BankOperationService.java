package service;

import lombok.AccessLevel;
import lombok.NoArgsConstructor;
import model.Account;
import model.OperationFactory;
import model.OperationType;

@NoArgsConstructor(access = AccessLevel.PRIVATE)
public final class BankOperationService {
    private static final String ACCOUNT_OPERATION_HISTORY = "Account operation history : ";
    private static final OperationFactory OPERATION_FACTORY = new OperationFactory();

    public static void deposit(Account account, double amount) {
        BankOperationValidator.depositValidation(account, amount);
        account.setBalance(account.getBalance() + amount);

        account.getOperations().add(OPERATION_FACTORY.getBankOperation(OperationType.DEPOSIT, amount, account.getBalance()));
    }

    public static void withdrawal(Account account, double amount) {
        BankOperationValidator.withdrawalValidation(account, amount);
        account.setBalance(account.getBalance() - amount);

        account.getOperations().add(OPERATION_FACTORY.getBankOperation(OperationType.WITHDRAWAL, amount, account.getBalance()));
    }

    public static String checkHistory(Account account) {
        return ACCOUNT_OPERATION_HISTORY + account.getOperations().toString();
    }


}
