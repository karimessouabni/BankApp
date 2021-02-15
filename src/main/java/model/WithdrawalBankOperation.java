package model;


import java.time.LocalDateTime;

// using java 14 record for the following immutable object
public record WithdrawalBankOperation(OperationType operationType, double amount, LocalDateTime date,
                                      double balance) implements BankOperation {
}
