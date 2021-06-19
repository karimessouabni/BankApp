package model;


import lombok.Data;

import java.time.LocalDate;


@Data
public class MuchBetterClient {
    private String email;
    private Account account;
    private String firstName;
    private String lastName;
    private LocalDate birthDate;
    private String address;
}
