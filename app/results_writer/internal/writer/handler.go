package writer

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"strconv"
	"strings"
	"time"
)

const (
	transactionInsertColumns = "trace_id, transaction_id, user_id, amount, currency, country, channel, fraud_score, is_fraud, decision, processed_at, ingested_at"
	maxRowsPerStatement      = 5000
	maxTraceSamples          = 20
)

const schemaSQL = `
CREATE TABLE IF NOT EXISTS transactions (
    id             SERIAL PRIMARY KEY,
    transaction_id VARCHAR(255) UNIQUE NOT NULL,
    user_id        VARCHAR(255),
    amount         NUMERIC(15, 2),
    currency       VARCHAR(10),
    country        VARCHAR(100),
    channel        VARCHAR(50),
    fraud_score    FLOAT,
    is_fraud       BOOLEAN,
    decision       VARCHAR(20),
    processed_at   TIMESTAMPTZ DEFAULT NOW(),
    trace_id       VARCHAR(128),
    ingested_at    TIMESTAMPTZ
);
ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS trace_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_tx_processed_at ON transactions (processed_at DESC);
CREATE INDEX IF NOT EXISTS idx_tx_is_fraud     ON transactions (is_fraud);
CREATE INDEX IF NOT EXISTS idx_tx_user_id      ON transactions (user_id);
CREATE INDEX IF NOT EXISTS idx_tx_trace_id     ON transactions (trace_id);
CREATE INDEX IF NOT EXISTS idx_tx_ingested_at  ON transactions (ingested_at DESC);
`

type Event struct {
	Records []Record `json:"Records"`
}

type Record struct {
	Body string `json:"body"`
}

type Result struct {
	Processed int `json:"processed"`
	Inserted  int `json:"inserted"`
	Skipped   int `json:"skipped"`
}

type payload struct {
	TraceID       *string    `json:"trace_id,omitempty"`
	TransactionID string     `json:"transaction_id"`
	UserID        *string    `json:"user_id,omitempty"`
	Amount        *float64   `json:"amount,omitempty"`
	Currency      *string    `json:"currency,omitempty"`
	Country       *string    `json:"country,omitempty"`
	Channel       *string    `json:"channel,omitempty"`
	FraudScore    *float64   `json:"fraud_score,omitempty"`
	IsFraud       *bool      `json:"is_fraud,omitempty"`
	ProcessedAt   *time.Time `json:"processed_at,omitempty"`
	IngestedAt    *time.Time `json:"ingested_at,omitempty"`
}

func Handle(ctx context.Context, db *sql.DB, event Event) (Result, error) {
	return HandleWithRequest(ctx, db, event, "", nil)
}

func HandleWithRequest(ctx context.Context, db *sql.DB, event Event, awsRequestID string, logger *log.Logger) (Result, error) {
	started := time.Now()
	if len(event.Records) == 0 {
		logWriter(logger, "results_batch_empty", map[string]any{
			"aws_request_id": awsRequestID,
			"duration_ms":    durationMS(started),
		})
		return Result{Processed: 0}, nil
	}

	rows := make([]payload, 0, len(event.Records))
	for idx, record := range event.Records {
		row, err := decodeRecordBody(record.Body)
		if err != nil {
			return Result{}, fmt.Errorf("record %d: %w", idx, err)
		}
		rows = append(rows, row)
	}

	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return Result{}, fmt.Errorf("begin transaction: %w", err)
	}
	defer func() {
		_ = tx.Rollback()
	}()

	if _, err := tx.ExecContext(ctx, schemaSQL); err != nil {
		return Result{}, fmt.Errorf("ensure schema: %w", err)
	}

	inserted, err := insertRows(ctx, tx, rows)
	if err != nil {
		return Result{}, err
	}

	if err := tx.Commit(); err != nil {
		return Result{}, fmt.Errorf("commit transaction: %w", err)
	}

	result := Result{
		Processed: len(rows),
		Inserted:  inserted,
		Skipped:   len(rows) - inserted,
	}
	logResultsBatchCommitted(logger, awsRequestID, rows, result, durationMS(started))
	return result, nil
}

func decodeRecordBody(body string) (payload, error) {
	if maybeSNS, ok := unwrapSNSBody(body); ok {
		body = maybeSNS
	}

	var row payload
	if err := json.Unmarshal([]byte(body), &row); err != nil {
		return payload{}, fmt.Errorf("decode JSON body: %w", err)
	}
	if row.TransactionID == "" {
		return payload{}, errors.New("transaction_id is required")
	}
	row.TraceID = normalizeTraceID(row.TraceID, row.TransactionID)

	return row, nil
}

func unwrapSNSBody(body string) (string, bool) {
	var envelope struct {
		Message string `json:"Message"`
	}
	if err := json.Unmarshal([]byte(body), &envelope); err != nil {
		return "", false
	}
	if envelope.Message == "" {
		return "", false
	}

	return envelope.Message, true
}

func insertRows(ctx context.Context, tx *sql.Tx, rows []payload) (int, error) {
	inserted := int64(0)
	for start := 0; start < len(rows); start += maxRowsPerStatement {
		end := start + maxRowsPerStatement
		if end > len(rows) {
			end = len(rows)
		}

		query, args := buildInsertStatement(rows[start:end])
		result, err := tx.ExecContext(ctx, query, args...)
		if err != nil {
			return 0, fmt.Errorf("insert rows %d-%d: %w", start, end, err)
		}
		if affected, err := result.RowsAffected(); err == nil {
			inserted += affected
		}
	}

	return int(inserted), nil
}

func buildInsertStatement(rows []payload) (string, []any) {
	var b strings.Builder
	b.Grow(len(rows) * 96)

	b.WriteString("INSERT INTO transactions (")
	b.WriteString(transactionInsertColumns)
	b.WriteString(") VALUES ")

	args := make([]any, 0, len(rows)*12)
	for idx, row := range rows {
		if idx > 0 {
			b.WriteByte(',')
		}

		base := idx*12 + 1
		b.WriteString("(")
		b.WriteString("$")
		b.WriteString(strconv.Itoa(base))
		b.WriteString(", $")
		b.WriteString(strconv.Itoa(base + 1))
		b.WriteString(", $")
		b.WriteString(strconv.Itoa(base + 2))
		b.WriteString(", $")
		b.WriteString(strconv.Itoa(base + 3))
		b.WriteString(", $")
		b.WriteString(strconv.Itoa(base + 4))
		b.WriteString(", $")
		b.WriteString(strconv.Itoa(base + 5))
		b.WriteString(", $")
		b.WriteString(strconv.Itoa(base + 6))
		b.WriteString(", $")
		b.WriteString(strconv.Itoa(base + 7))
		b.WriteString(", $")
		b.WriteString(strconv.Itoa(base + 8))
		b.WriteString(", $")
		b.WriteString(strconv.Itoa(base + 9))
		b.WriteString(", COALESCE($")
		b.WriteString(strconv.Itoa(base + 10))
		b.WriteString("::timestamptz, NOW()), $")
		b.WriteString(strconv.Itoa(base + 11))
		b.WriteString(")")

		args = append(args,
			nullableString(row.TraceID),
			row.TransactionID,
			nullableString(row.UserID),
			nullableFloat64(row.Amount),
			nullableString(row.Currency),
			nullableString(row.Country),
			nullableString(row.Channel),
			nullableFloat64(row.FraudScore),
			boolValue(row.IsFraud),
			decisionValue(row.IsFraud),
			nullableTime(row.ProcessedAt),
			nullableTime(row.IngestedAt),
		)
	}

	b.WriteString(" ON CONFLICT (transaction_id) DO NOTHING")

	return b.String(), args
}

func nullableString(value *string) any {
	if value == nil {
		return nil
	}
	return *value
}

func normalizeTraceID(value *string, transactionID string) *string {
	if value != nil && isSafeTraceID(*value) {
		normalized := strings.TrimSpace(*value)
		return &normalized
	}
	if transactionID == "" {
		return nil
	}
	fallback := "legacy-" + hashIdentifier(transactionID)
	return &fallback
}

func hashIdentifier(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])[:32]
}

func isSafeTraceID(value string) bool {
	value = strings.TrimSpace(value)
	if len(value) == 0 || len(value) > 128 {
		return false
	}
	if isUUIDTraceID(value) {
		return true
	}
	if strings.HasPrefix(value, "legacy-") && len(value) == len("legacy-")+32 {
		return isLowerHex(value[len("legacy-"):])
	}
	if strings.HasPrefix(value, "sqs-") && len(value) == len("sqs-")+32 {
		return isLowerHex(value[len("sqs-"):])
	}
	return false
}

func isUUIDTraceID(value string) bool {
	if len(value) != 36 {
		return false
	}
	for idx, char := range value {
		switch idx {
		case 8, 13, 18, 23:
			if char != '-' {
				return false
			}
		default:
			if !((char >= '0' && char <= '9') || (char >= 'a' && char <= 'f') || (char >= 'A' && char <= 'F')) {
				return false
			}
		}
	}
	return true
}

func isLowerHex(value string) bool {
	for _, char := range value {
		if !((char >= '0' && char <= '9') || (char >= 'a' && char <= 'f')) {
			return false
		}
	}
	return true
}

func nullableFloat64(value *float64) any {
	if value == nil {
		return nil
	}
	return *value
}

func nullableTime(value *time.Time) any {
	if value == nil {
		return nil
	}
	return *value
}

func boolValue(value *bool) bool {
	if value == nil {
		return false
	}
	return *value
}

func decisionValue(isFraud *bool) string {
	if boolValue(isFraud) {
		return "block"
	}
	return "allow"
}

func logResultsBatchCommitted(logger *log.Logger, awsRequestID string, rows []payload, result Result, duration int64) {
	traceCount, traceSamples := traceSummary(rows)
	latencyCount, minLatency, maxLatency, avgLatency := latencySummary(rows, time.Now().UTC())
	logWriter(logger, "results_batch_committed", map[string]any{
		"aws_request_id":       awsRequestID,
		"record_count":         result.Processed,
		"inserted_count":       result.Inserted,
		"skipped_count":        result.Skipped,
		"trace_id_count":       traceCount,
		"trace_id_samples":     traceSamples,
		"latency_sample_count": latencyCount,
		"latency_ms_min":       minLatency,
		"latency_ms_max":       maxLatency,
		"latency_ms_avg":       avgLatency,
		"duration_ms":          duration,
	})
}

func traceSummary(rows []payload) (int, []string) {
	seen := map[string]struct{}{}
	samples := []string{}
	for _, row := range rows {
		if row.TraceID == nil || *row.TraceID == "" {
			continue
		}
		traceID := *row.TraceID
		if _, ok := seen[traceID]; ok {
			continue
		}
		seen[traceID] = struct{}{}
		if len(samples) < maxTraceSamples {
			samples = append(samples, traceID)
		}
	}
	return len(seen), samples
}

func latencySummary(rows []payload, now time.Time) (int, int64, int64, int64) {
	count := 0
	var minValue, maxValue, total int64
	for _, row := range rows {
		if row.IngestedAt == nil {
			continue
		}
		latency := now.Sub(row.IngestedAt.UTC()).Milliseconds()
		if latency < 0 {
			latency = 0
		}
		if count == 0 || latency < minValue {
			minValue = latency
		}
		if latency > maxValue {
			maxValue = latency
		}
		total += latency
		count++
	}
	if count == 0 {
		return 0, 0, 0, 0
	}
	return count, minValue, maxValue, total / int64(count)
}

func logWriter(logger *log.Logger, action string, fields map[string]any) {
	if logger == nil {
		return
	}
	record := writerLogRecord(action, fields)
	encoded, err := json.Marshal(record)
	if err != nil {
		logger.Print(`{"level":"ERROR","component":"results_writer","action":"log_encode_failed"}`)
		return
	}
	logger.Print(string(encoded))
}

func writerLogRecord(action string, fields map[string]any) map[string]any {
	level := "INFO"
	if fields != nil {
		if customLevel, ok := fields["level"].(string); ok && customLevel != "" {
			level = customLevel
		}
	}
	record := map[string]any{
		"level":     level,
		"component": "results_writer",
		"action":    action,
	}
	for key, value := range fields {
		if key == "level" || value == nil || isSensitiveLogKey(key) {
			continue
		}
		record[key] = value
	}
	return record
}

func isSensitiveLogKey(key string) bool {
	switch key {
	case "transaction_id", "user_id", "amount", "currency", "country", "channel",
		"destination_account", "fraud_score", "decision", "payload", "body",
		"receipt", "receipt_handle", "raw_receipt_handle":
		return true
	default:
		return false
	}
}

func durationMS(started time.Time) int64 {
	return time.Since(started).Milliseconds()
}

func errorClass(err error) string {
	if err == nil {
		return ""
	}
	return fmt.Sprintf("%T", err)
}
