package writer

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestDecodeRecordBodyRawJSON(t *testing.T) {
	t.Parallel()

	row, err := decodeRecordBody(`{"transaction_id":"tx-1","trace_id":"11111111-1111-4111-8111-111111111111","user_id":"user-1","is_fraud":true}`)
	if err != nil {
		t.Fatalf("decodeRecordBody() error = %v", err)
	}

	if row.TransactionID != "tx-1" {
		t.Fatalf("TransactionID = %q, want tx-1", row.TransactionID)
	}
	if row.TraceID == nil || *row.TraceID != "11111111-1111-4111-8111-111111111111" {
		t.Fatalf("TraceID = %#v, want UUID trace", row.TraceID)
	}
	if row.UserID == nil || *row.UserID != "user-1" {
		t.Fatalf("UserID = %#v, want user-1", row.UserID)
	}
	if row.IsFraud == nil || !*row.IsFraud {
		t.Fatalf("IsFraud = %#v, want true", row.IsFraud)
	}
}

func TestDecodeRecordBodyNormalizesUnsafeTraceID(t *testing.T) {
	t.Parallel()

	row, err := decodeRecordBody(`{"transaction_id":"tx-secret","trace_id":"tx-secret"}`)
	if err != nil {
		t.Fatalf("decodeRecordBody() error = %v", err)
	}

	if row.TraceID == nil || *row.TraceID == "" {
		t.Fatal("TraceID is empty")
	}
	if *row.TraceID == "tx-secret" {
		t.Fatal("unsafe trace_id was preserved")
	}
}

func TestDecodeRecordBodySNSWrapper(t *testing.T) {
	t.Parallel()

	row, err := decodeRecordBody(`{"Type":"Notification","Message":"{\"transaction_id\":\"tx-2\",\"amount\":12.5,\"currency\":\"ARS\"}"}`)
	if err != nil {
		t.Fatalf("decodeRecordBody() error = %v", err)
	}

	if row.TransactionID != "tx-2" {
		t.Fatalf("TransactionID = %q, want tx-2", row.TransactionID)
	}
	if row.Amount == nil || *row.Amount != 12.5 {
		t.Fatalf("Amount = %#v, want 12.5", row.Amount)
	}
	if row.Currency == nil || *row.Currency != "ARS" {
		t.Fatalf("Currency = %#v, want ARS", row.Currency)
	}
}

func TestBuildInsertStatementChunksRows(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, time.May, 18, 12, 0, 0, 0, time.UTC)
	user := "user-1"
	currency := "USD"
	trace := "trace-1"
	ingestedAt := now.Add(-5 * time.Second)
	rows := []payload{
		{
			TraceID:       &trace,
			TransactionID: "tx-1",
			UserID:        &user,
			Currency:      &currency,
			IsFraud:       boolPtr(true),
			ProcessedAt:   &now,
			IngestedAt:    &ingestedAt,
		},
		{
			TransactionID: "tx-2",
			IsFraud:       boolPtr(false),
		},
	}

	query, args := buildInsertStatement(rows)

	if !strings.HasPrefix(query, "INSERT INTO transactions (trace_id, transaction_id, user_id, amount, currency, country, channel, fraud_score, is_fraud, decision, processed_at, ingested_at) VALUES ") {
		t.Fatalf("query prefix mismatch: %s", query)
	}
	if !strings.HasSuffix(query, "ON CONFLICT (transaction_id) DO NOTHING") {
		t.Fatalf("query suffix mismatch: %s", query)
	}
	if got, want := len(args), 24; got != want {
		t.Fatalf("len(args) = %d, want %d", got, want)
	}
	if got, want := args[0], "trace-1"; got != want {
		t.Fatalf("trace arg 1 = %v, want %v", got, want)
	}
	if got, want := args[9], "block"; got != want {
		t.Fatalf("decision arg 1 = %v, want %v", got, want)
	}
	if got, want := args[21], "allow"; got != want {
		t.Fatalf("decision arg 2 = %v, want %v", got, want)
	}
}

func boolPtr(value bool) *bool {
	return &value
}

func TestWriterLogRecordOmitsSensitiveFields(t *testing.T) {
	record := writerLogRecord("results_batch_committed", map[string]any{
		"trace_id_samples":    []string{"trace-safe"},
		"transaction_id":      "tx-secret",
		"user_id":             "user-secret",
		"amount":              99.5,
		"currency":            "ARS",
		"country":             "AR",
		"channel":             "web",
		"destination_account": "dest-secret",
		"fraud_score":         0.98,
		"decision":            "block",
		"receipt_handle":      "receipt-secret",
		"record_count":        2,
	})

	encoded, err := json.Marshal(record)
	if err != nil {
		t.Fatalf("json.Marshal(record) error = %v", err)
	}
	body := string(encoded)
	for _, forbidden := range []string{
		"tx-secret", "user-secret", "99.5", "ARS", "AR", "web",
		"dest-secret", "0.98", "block", "receipt-secret",
		"transaction_id", "user_id", "fraud_score",
	} {
		if strings.Contains(body, forbidden) {
			t.Fatalf("log record leaked %q: %s", forbidden, body)
		}
	}
	if !strings.Contains(body, "trace-safe") {
		t.Fatalf("log record missing trace sample: %s", body)
	}
}

func TestTraceSummaryCapsSamples(t *testing.T) {
	rows := make([]payload, 0, maxTraceSamples+2)
	for idx := range maxTraceSamples+2 {
		traceID := "trace-" + string(rune('a'+idx))
		rows = append(rows, payload{TraceID: &traceID})
	}

	count, samples := traceSummary(rows)
	if got, want := count, maxTraceSamples+2; got != want {
		t.Fatalf("trace count = %d, want %d", got, want)
	}
	if got, want := len(samples), maxTraceSamples; got != want {
		t.Fatalf("sample count = %d, want %d", got, want)
	}
}
