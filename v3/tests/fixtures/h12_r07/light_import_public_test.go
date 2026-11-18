package stable
import "testing"
func TestPublicLegacyImport(t *testing.T) {
    if LegacyName() != 7 { t.Fatal("existing exported behavior changed") }
}
