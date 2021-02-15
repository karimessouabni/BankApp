package service;

import exception.AccountBlockedException;
import exception.InvalidOperationAmountException;
import exception.MinBalanceExceededException;
import lombok.AccessLevel;
import lombok.NoArgsConstructor;
import model.Account;

@NoArgsConstructor(access = AccessLevel.PRIVATE)
final class BankOperationValidator {

    static void withdrawalValidation(Account account, double amount) {
        basicValidation(account, amount);
        if (account.getBalance() - amount < account.getMinBalance()) {
            throw new MinBalanceExceededException("Withdrawal rejected : Min balance exceeded !");
        }
    }

    static void depositValidation(Account account, double amount) {
        basicValidation(account, amount);
    }


    private static void basicValidation(Account account, double amount) {
        if (account.isBlocked()) {
            throw new AccountBlockedException("Account is blocked. Cannot make a deposit !");
        } else if (amount < 0) {
            throw new InvalidOperationAmountException("To make a deposit, the amount should be > 0");
        }
    }

}
