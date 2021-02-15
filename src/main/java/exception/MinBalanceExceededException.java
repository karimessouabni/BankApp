package exception;

public class MinBalanceExceededException extends RuntimeException {
    public MinBalanceExceededException(String message) {
        super(message);
    }
}
