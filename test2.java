@Service
public class BucketTxService {

    private final BucketRepository bucketRepository;

    public BucketTxService(BucketRepository bucketRepository) {
        this.bucketRepository = bucketRepository;
    }

    @Transactional
    public boolean claim(Long id) {
        return bucketRepository.claim(id) == 1;
    }

    @Transactional
    public void markDone(Long id) {
        bucketRepository.markDone(id);
    }

    @Transactional
    public void markFailed(Long id, String err) {
        bucketRepository.markFailed(id, err);
    }
}