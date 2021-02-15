package service;

import model.Account;
import model.Client;
import org.junit.jupiter.api.BeforeEach;

import java.time.LocalDate;

public class ServiceTestInit {

    protected Client client;

    @BeforeEach
    void init() {
        Account account = Account.builder()
                .minBalance(-100d)
                .balance(2000d)
                .build();

        client = Client.builder()
                .account(account)
                .birthDate(LocalDate.of(1992, 9, 11))
                .address("this is my test address")
                .email("test@email.fr")
                .firstName("karim")
                .lastName("es-souabni")
                .build();
    }
}
