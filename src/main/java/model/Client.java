package model;

import lombok.Builder;
import lombok.Data;

import java.time.LocalDate;


@Data
@Builder
public class Client {

    private final String email;
    private String firstName;
    private String lastName;
    private LocalDate birthDate;
    private String address; // this could be an Address Object instead of String
    private Account account;

}
