package exception;

public class InvalidOperationAmountException extends RuntimeException {
    public InvalidOperationAmountException(String message) {
        super(message);
    }
}
