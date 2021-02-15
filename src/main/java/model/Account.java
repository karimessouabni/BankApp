package model;

import lombok.Builder;
import lombok.Data;

import java.util.HashSet;
import java.util.Set;

@Data
@Builder
public class Account {

    @Builder.Default
    private double balance = 0d;
    @Builder.Default
    private double minBalance = 0d;
    @Builder.Default
    private boolean blocked = false;
    @Builder.Default
    private Set<BankOperation> operations = new HashSet<>();

}