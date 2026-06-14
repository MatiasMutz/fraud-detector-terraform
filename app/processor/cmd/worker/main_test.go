package main

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/FSendot/fraud-detector/processor/internal/dynamo"
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/sqs"
	sqstypes "github.com/aws/aws-sdk-go-v2/service/sqs/types"
)

func TestTransactionJSONContract(t *testing.T) {
	body := `{
		"transaction_id": "tx_1",
		"trace_id": "11111111-1111-4111-8111-111111111111",
		"user_id": "user_1",
		"amount": 42.5,
		"currency": "ARS",
		"timestamp": "2026-04-03T10:22:00Z",
		"channel": "web",
		"destination_account": "dest_1",
		"country": "AR",
		"oldbalance_org": 100.0,
		"newbalance_orig": 57.5
	}`

	var tx Transaction
	if err := json.Unmarshal([]byte(body), &tx); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}
	if tx.TransactionID != "tx_1" {
		t.Fatalf("transaction_id = %q, want tx_1", tx.TransactionID)
	}
	if tx.TraceID != "11111111-1111-4111-8111-111111111111" {
		t.Fatalf("trace_id = %q, want UUID trace", tx.TraceID)
	}
	if tx.UserID != "user_1" {
		t.Fatalf("user_id = %q, want user_1", tx.UserID)
	}
	if tx.OldBalanceOrg == nil || *tx.OldBalanceOrg != 100.0 {
		t.Fatalf("oldbalance_org = %v, want 100", tx.OldBalanceOrg)
	}
	if tx.NewBalanceOrig == nil || *tx.NewBalanceOrig != 57.5 {
		t.Fatalf("newbalance_orig = %v, want 57.5", tx.NewBalanceOrig)
	}
}

func TestBuildMLFeaturesInitializesContractFields(t *testing.T) {
	oldBalance := 100.0
	newBalance := 57.5
	tx := Transaction{
		TransactionID:  "tx_1",
		Amount:         42.5,
		OldBalanceOrg:  &oldBalance,
		NewBalanceOrig: &newBalance,
	}

	features := buildMLFeatures(tx, []string{
		"amount",
		"amount_log1p",
		"oldbalance_org",
		"newbalance_orig",
		"balance_delta_org",
		"card1",
	})

	if got := features["amount"]; got != 42.5 {
		t.Fatalf("amount = %v, want 42.5", got)
	}
	if got := features["oldbalance_org"]; got != 100.0 {
		t.Fatalf("oldbalance_org = %v, want 100", got)
	}
	if got := features["balance_delta_org"]; got != -42.5 {
		t.Fatalf("balance_delta_org = %v, want -42.5", got)
	}
	if value := features["card1"]; !math.IsNaN(value) {
		t.Fatalf("card1 = %v, want NaN placeholder", value)
	}
}

func TestProcessMessageSendsEveryResultToResultsQueue(t *testing.T) {
	queue := &fakeQueueClient{}
	store := &fakeProfileStore{}
	engine := fakeScorer{fraudScore: 0.42, isFraud: false}
	msg := workerMessage(`{
		"transaction_id": "tx_ok",
		"trace_id": "22222222-2222-4222-8222-222222222222",
		"user_id": "user_1",
		"amount": 42.5,
		"currency": "ARS",
		"timestamp": "2026-04-03T10:22:00Z",
		"channel": "web",
		"destination_account": "dest_1",
		"country": "AR"
	}`)

	err := processMessage(context.Background(), msg, queue, store, nil, engine, &userLockSet{}, "ingestion-url", "results-url", "fraud-url")
	if err != nil {
		t.Fatalf("processMessage() error = %v", err)
	}
	if len(queue.sent) != 1 {
		t.Fatalf("sent messages = %d, want 1", len(queue.sent))
	}
	if queue.sent[0].queueURL != "results-url" {
		t.Fatalf("sent queue = %q, want results-url", queue.sent[0].queueURL)
	}
	result := decodeResult(t, queue.sent[0].body)
	if result.TransactionID != "tx_ok" || result.IsFraud {
		t.Fatalf("result = %+v, want tx_ok non-fraud", result)
	}
	if result.TraceID != "22222222-2222-4222-8222-222222222222" {
		t.Fatalf("trace_id = %q, want UUID trace", result.TraceID)
	}
	if _, err := time.Parse(time.RFC3339, result.ProcessedAt); err != nil {
		t.Fatalf("processed_at = %q, want RFC3339 timestamp: %v", result.ProcessedAt, err)
	}
	if _, err := time.Parse(time.RFC3339, result.IngestedAt); err != nil {
		t.Fatalf("ingested_at = %q, want RFC3339 timestamp: %v", result.IngestedAt, err)
	}
	if len(queue.deleted) != 1 || queue.deleted[0].queueURL != "ingestion-url" {
		t.Fatalf("deleted = %+v, want original ingestion message deleted", queue.deleted)
	}
}

func TestProcessMessageSendsFraudResultToBothOutputQueues(t *testing.T) {
	queue := &fakeQueueClient{}
	store := &fakeProfileStore{}
	engine := fakeScorer{fraudScore: 0.98, isFraud: true}
	msg := workerMessage(`{
		"transaction_id": "tx_fraud",
		"trace_id": "33333333-3333-4333-8333-333333333333",
		"user_id": "user_1",
		"amount": 9000,
		"currency": "ARS",
		"timestamp": "2026-04-03T10:22:00Z",
		"channel": "web",
		"destination_account": "dest_1",
		"country": "AR"
	}`)

	err := processMessage(context.Background(), msg, queue, store, nil, engine, &userLockSet{}, "ingestion-url", "results-url", "fraud-url")
	if err != nil {
		t.Fatalf("processMessage() error = %v", err)
	}
	if len(queue.sent) != 2 {
		t.Fatalf("sent messages = %d, want 2", len(queue.sent))
	}
	if queue.sent[0].queueURL != "results-url" {
		t.Fatalf("first sent queue = %q, want results-url", queue.sent[0].queueURL)
	}
	if queue.sent[1].queueURL != "fraud-url" {
		t.Fatalf("second sent queue = %q, want fraud-url", queue.sent[1].queueURL)
	}
	if queue.sent[0].body != queue.sent[1].body {
		t.Fatal("results and fraud alert payloads differ")
	}
	result := decodeResult(t, queue.sent[1].body)
	if !result.IsFraud || result.FraudScore != 0.98 {
		t.Fatalf("fraud result = %+v, want fraud score 0.98", result)
	}
	if result.TraceID != "33333333-3333-4333-8333-333333333333" {
		t.Fatalf("trace_id = %q, want UUID trace", result.TraceID)
	}
	if len(queue.deleted) != 1 {
		t.Fatalf("deleted messages = %d, want 1", len(queue.deleted))
	}
}

func TestProcessMessageKeepsOriginalMessageWhenFraudAlertSendFails(t *testing.T) {
	sendErr := errors.New("sqs unavailable")
	queue := &fakeQueueClient{sendErrorsByQueue: map[string]error{"fraud-url": sendErr}}
	store := &fakeProfileStore{}
	engine := fakeScorer{fraudScore: 0.98, isFraud: true}
	msg := workerMessage(`{
		"transaction_id": "tx_retry",
		"trace_id": "44444444-4444-4444-8444-444444444444",
		"user_id": "user_1",
		"amount": 9000,
		"currency": "ARS",
		"timestamp": "2026-04-03T10:22:00Z",
		"channel": "web",
		"destination_account": "dest_1",
		"country": "AR"
	}`)

	err := processMessage(context.Background(), msg, queue, store, nil, engine, &userLockSet{}, "ingestion-url", "results-url", "fraud-url")
	if !errors.Is(err, sendErr) {
		t.Fatalf("processMessage() error = %v, want %v", err, sendErr)
	}
	if len(queue.sent) != 1 || queue.sent[0].queueURL != "results-url" {
		t.Fatalf("sent = %+v, want only successful results send recorded", queue.sent)
	}
	if len(queue.deleted) != 0 {
		t.Fatalf("deleted messages = %d, want 0", len(queue.deleted))
	}
}

func TestProcessMessageGeneratesPrivacySafeFallbackTraceID(t *testing.T) {
	queue := &fakeQueueClient{}
	store := &fakeProfileStore{}
	engine := fakeScorer{fraudScore: 0.42, isFraud: false}
	msg := workerMessage(`{
		"transaction_id": "tx_without_trace",
		"user_id": "user_1",
		"amount": 42.5,
		"currency": "ARS",
		"timestamp": "2026-04-03T10:22:00Z",
		"channel": "web",
		"destination_account": "dest_1",
		"country": "AR"
	}`)

	err := processMessage(context.Background(), msg, queue, store, nil, engine, &userLockSet{}, "ingestion-url", "results-url", "fraud-url")
	if err != nil {
		t.Fatalf("processMessage() error = %v", err)
	}
	result := decodeResult(t, queue.sent[0].body)
	if result.TraceID == "" {
		t.Fatal("trace_id is empty")
	}
	if result.TraceID == "tx_without_trace" {
		t.Fatal("fallback trace_id leaked the raw transaction_id")
	}
	if got, want := result.TraceID, ensureTraceID(Transaction{TransactionID: "tx_without_trace"}, msg); got != want {
		t.Fatalf("trace_id = %q, want %q", got, want)
	}
}

func TestProcessMessageRejectsUnsafeProvidedTraceID(t *testing.T) {
	queue := &fakeQueueClient{}
	store := &fakeProfileStore{}
	engine := fakeScorer{fraudScore: 0.42, isFraud: false}
	msg := workerMessage(`{
		"trace_id": "tx_secret_as_trace",
		"transaction_id": "tx_secret_as_trace",
		"user_id": "user_1",
		"amount": 42.5,
		"currency": "ARS",
		"timestamp": "2026-04-03T10:22:00Z",
		"channel": "web",
		"destination_account": "dest_1",
		"country": "AR"
	}`)

	err := processMessage(context.Background(), msg, queue, store, nil, engine, &userLockSet{}, "ingestion-url", "results-url", "fraud-url")
	if err != nil {
		t.Fatalf("processMessage() error = %v", err)
	}
	result := decodeResult(t, queue.sent[0].body)
	if result.TraceID == "tx_secret_as_trace" {
		t.Fatal("unsafe provided trace_id was propagated")
	}
	if got, want := result.TraceID, ensureTraceID(Transaction{TraceID: "tx_secret_as_trace", TransactionID: "tx_secret_as_trace"}, msg); got != want {
		t.Fatalf("trace_id = %q, want %q", got, want)
	}
}

func TestProcessorLogRecordOmitsSensitiveFields(t *testing.T) {
	record := processorLogRecord("tx_processed", map[string]any{
		"trace_id":            "trace-safe",
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
		"duration_ms":         10,
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
		t.Fatalf("log record missing trace id: %s", body)
	}
}

type fakeScorer struct {
	fraudScore float64
	isFraud    bool
}

func (s fakeScorer) score(context.Context, Transaction, *dynamo.UserProfile) (float64, bool) {
	return s.fraudScore, s.isFraud
}

type fakeProfileStore struct {
	profile *dynamo.UserProfile
}

func (s *fakeProfileStore) GetProfile(_ context.Context, userID string) (*dynamo.UserProfile, error) {
	if s.profile != nil {
		return s.profile, nil
	}
	return dynamo.NewDefaultProfile(userID), nil
}

func (s *fakeProfileStore) UpdateProfile(context.Context, *dynamo.UserProfile, float64, string, string, string, string) error {
	return nil
}

type sentMessage struct {
	queueURL string
	body     string
}

type deletedMessage struct {
	queueURL      string
	receiptHandle string
}

type fakeQueueClient struct {
	sent              []sentMessage
	deleted           []deletedMessage
	sendErrorsByQueue map[string]error
}

func (c *fakeQueueClient) ReceiveMessage(context.Context, *sqs.ReceiveMessageInput, ...func(*sqs.Options)) (*sqs.ReceiveMessageOutput, error) {
	return &sqs.ReceiveMessageOutput{}, nil
}

func (c *fakeQueueClient) DeleteMessage(_ context.Context, input *sqs.DeleteMessageInput, _ ...func(*sqs.Options)) (*sqs.DeleteMessageOutput, error) {
	c.deleted = append(c.deleted, deletedMessage{
		queueURL:      aws.ToString(input.QueueUrl),
		receiptHandle: aws.ToString(input.ReceiptHandle),
	})
	return &sqs.DeleteMessageOutput{}, nil
}

func (c *fakeQueueClient) SendMessage(_ context.Context, input *sqs.SendMessageInput, _ ...func(*sqs.Options)) (*sqs.SendMessageOutput, error) {
	queueURL := aws.ToString(input.QueueUrl)
	if err := c.sendErrorsByQueue[queueURL]; err != nil {
		return nil, err
	}
	c.sent = append(c.sent, sentMessage{
		queueURL: queueURL,
		body:     aws.ToString(input.MessageBody),
	})
	return &sqs.SendMessageOutput{}, nil
}

func workerMessage(body string) sqstypes.Message {
	return sqstypes.Message{
		Body:          aws.String(body),
		MessageId:     aws.String("message-1"),
		ReceiptHandle: aws.String("receipt-1"),
		Attributes: map[string]string{
			"ApproximateReceiveCount": "1",
			"SentTimestamp":           "1775211720000",
		},
	}
}

func decodeResult(t *testing.T, body string) ScoringResult {
	t.Helper()

	var result ScoringResult
	if err := json.Unmarshal([]byte(body), &result); err != nil {
		t.Fatalf("json.Unmarshal(result) error = %v", err)
	}
	return result
}
