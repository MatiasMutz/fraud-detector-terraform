package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	fraudruntime "github.com/FSendot/fraud-detector/net/serving/go/pkg/fraudruntime"
	"github.com/FSendot/fraud-detector/processor/internal/dynamo"
	"github.com/FSendot/fraud-detector/processor/internal/scoring"
	"github.com/FSendot/fraud-detector/processor/internal/store"
	"github.com/aws/aws-sdk-go-v2/aws"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	dynamosvc "github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/sqs"
	sqstypes "github.com/aws/aws-sdk-go-v2/service/sqs/types"
)

// Transaction is the JSON payload expected from the SQS queue.
// Balance fields are optional: when absent the ML engine applies its
// MissingGoToLeft strategy for those tree splits.
// Features allows callers to supply pre-computed ML features (e.g. card/addr/velocity
// signals). Values override NaN defaults for known feature-contract names; unknown
// names are silently ignored.
type Transaction struct {
	TraceID            string             `json:"trace_id,omitempty"`
	TransactionID      string             `json:"transaction_id"`
	UserID             string             `json:"user_id"`
	Amount             float64            `json:"amount"`
	Currency           string             `json:"currency"`
	Timestamp          string             `json:"timestamp"`
	Channel            string             `json:"channel"`
	DestinationAccount string             `json:"destination_account"`
	Country            string             `json:"country"`
	OldBalanceOrg      *float64           `json:"oldbalance_org,omitempty"`
	NewBalanceOrig     *float64           `json:"newbalance_orig,omitempty"`
	OldBalanceDest     *float64           `json:"oldbalance_dest,omitempty"`
	NewBalanceDest     *float64           `json:"newbalance_dest,omitempty"`
	Features           map[string]float64 `json:"features,omitempty"`
}

// ScoringResult is published to the results queues after scoring.
type ScoringResult struct {
	TraceID       string  `json:"trace_id,omitempty"`
	TransactionID string  `json:"transaction_id"`
	UserID        string  `json:"user_id"`
	Amount        float64 `json:"amount"`
	Currency      string  `json:"currency"`
	Country       string  `json:"country"`
	Channel       string  `json:"channel"`
	FraudScore    float64 `json:"fraud_score"`
	IsFraud       bool    `json:"is_fraud"`
	ProcessedAt   string  `json:"processed_at,omitempty"`
	IngestedAt    string  `json:"ingested_at,omitempty"`
}

// AuditEvent is written to S3 for full traceability.
type AuditEvent struct {
	Transaction   Transaction   `json:"transaction"`
	ScoringResult ScoringResult `json:"scoring_result"`
	TraceID       string        `json:"trace_id,omitempty"`
	ProcessedAt   string        `json:"processed_at"`
	IngestedAt    string        `json:"ingested_at,omitempty"`
}

type userLockSet struct {
	locks sync.Map
}

func (s *userLockSet) lock(userID string) func() {
	value, _ := s.locks.LoadOrStore(userID, &sync.Mutex{})
	mu := value.(*sync.Mutex)
	mu.Lock()
	return mu.Unlock
}

// scorer abstracts ML engine and rule-based fallback behind a single interface.
type scorer interface {
	score(ctx context.Context, tx Transaction, profile *dynamo.UserProfile) (fraudScore float64, isFraud bool)
}

type queueClient interface {
	ReceiveMessage(ctx context.Context, params *sqs.ReceiveMessageInput, optFns ...func(*sqs.Options)) (*sqs.ReceiveMessageOutput, error)
	DeleteMessage(ctx context.Context, params *sqs.DeleteMessageInput, optFns ...func(*sqs.Options)) (*sqs.DeleteMessageOutput, error)
	SendMessage(ctx context.Context, params *sqs.SendMessageInput, optFns ...func(*sqs.Options)) (*sqs.SendMessageOutput, error)
}

type profileStore interface {
	GetProfile(ctx context.Context, userID string) (*dynamo.UserProfile, error)
	UpdateProfile(ctx context.Context, profile *dynamo.UserProfile, amount float64, country, channel, destination, timestamp string) error
}

// mlScorer uses the trained Go runtime loaded from runtime_spec.json.
type mlScorer struct {
	inner *fraudruntime.Scorer
}

func (s *mlScorer) score(_ context.Context, tx Transaction, profile *dynamo.UserProfile) (float64, bool) {
	features := buildMLFeatures(tx, s.inner.Spec().FeatureContract.FeatureOrder)
	result, err := s.inner.ScoreOne(fraudruntime.ScoreInput{
		TransactionID: tx.TransactionID,
		Features:      features,
		Metadata: map[string]any{
			"trace_id": tx.TraceID,
		},
	})
	if err != nil {
		return rulesScore(tx, profile)
	}
	return result.CalibratedScore, result.PredictedLabel == 1
}

// rulesScorer applies the legacy rule-based engine when the ML model is unavailable.
type rulesScorer struct{}

func (r *rulesScorer) score(_ context.Context, tx Transaction, profile *dynamo.UserProfile) (float64, bool) {
	return rulesScore(tx, profile)
}

func rulesScore(tx Transaction, profile *dynamo.UserProfile) (float64, bool) {
	result := scoring.EvaluateDirect(tx.Amount, tx.Country, tx.DestinationAccount, profile)
	return float64(result.Score) / 100.0, result.Score >= 70
}

func main() {
	ctx := context.Background()

	cfg, err := awsconfig.LoadDefaultConfig(ctx)
	if err != nil {
		log.Fatalf("failed to load AWS config: %v", err)
	}

	dynamoClient := dynamo.NewClient(dynamosvc.NewFromConfig(cfg))
	sqsClient := sqs.NewFromConfig(cfg)

	var s3Client *store.S3Client
	if os.Getenv("S3_AUDIT_BUCKET") != "" {
		var err error
		s3Client, err = store.NewS3Client(cfg)
		if err != nil {
			logProcessor("s3_audit_init_failed", map[string]any{
				"level":       "WARN",
				"error_class": errorClass(err),
			})
		} else {
			logProcessor("s3_audit_enabled", nil)
		}
	}

	engine := resolveScorer()

	queueURL := mustEnv("QUEUE_URL")
	resultsQueueURL := mustEnv("RESULTS_QUEUE_URL")
	fraudAlertQueueURL := mustEnv("FRAUD_ALERT_QUEUE_URL")
	processorConcurrency := envInt("PROCESSOR_CONCURRENCY", 32, 1, 512)
	processorPollers := envInt("PROCESSOR_POLLERS", 4, 1, 64)

	logProcessor("worker_started", map[string]any{
		"concurrency": processorConcurrency,
		"pollers":     processorPollers,
	})
	runLoop(ctx, sqsClient, dynamoClient, s3Client, engine, queueURL, resultsQueueURL, fraudAlertQueueURL, processorConcurrency, processorPollers)
}

// resolveScorer loads the ML engine when runtime_spec.json is available,
// otherwise falls back to rule-based scoring with a startup warning.
func resolveScorer() scorer {
	specPath, err := scoring.ResolveRuntimeSpecPath()
	if err != nil {
		logProcessor("scoring_rules_fallback", map[string]any{
			"level":       "WARN",
			"error_class": errorClass(err),
		})
		return &rulesScorer{}
	}

	s, err := fraudruntime.NewScorerFromSpecPath(specPath)
	if err != nil {
		logProcessor("scoring_rules_fallback", map[string]any{
			"level":       "WARN",
			"error_class": errorClass(err),
		})
		return &rulesScorer{}
	}

	logProcessor("scoring_engine_loaded", map[string]any{
		"model_version": s.Spec().ModelVersion,
	})
	return &mlScorer{inner: s}
}

func runLoop(
	ctx context.Context,
	sqsClient queueClient,
	dynamoClient profileStore,
	s3Client *store.S3Client,
	engine scorer,
	queueURL, resultsQueueURL, fraudAlertQueueURL string,
	processorConcurrency, processorPollers int,
) {
	jobs := make(chan sqstypes.Message, processorConcurrency*10)
	userLocks := &userLockSet{}

	var workers sync.WaitGroup
	for range processorConcurrency {
		workers.Go(func() {
			for msg := range jobs {
				if err := processMessage(ctx, msg, sqsClient, dynamoClient, s3Client, engine, userLocks, queueURL, resultsQueueURL, fraudAlertQueueURL); err != nil {
					// Do not delete — visibility timeout expires and the message retries.
					// After maxReceiveCount it lands in the DLQ.
				}
			}
		})
	}

	for pollerID := range processorPollers {
		go func(pollerID int) {
			for {
				out, err := sqsClient.ReceiveMessage(ctx, &sqs.ReceiveMessageInput{
					QueueUrl:            aws.String(queueURL),
					MaxNumberOfMessages: 10,
					WaitTimeSeconds:     20,
					MessageSystemAttributeNames: []sqstypes.MessageSystemAttributeName{
						sqstypes.MessageSystemAttributeNameApproximateReceiveCount,
						sqstypes.MessageSystemAttributeNameSentTimestamp,
					},
				})
				if err != nil {
					logProcessor("sqs_receive_error", map[string]any{
						"level":       "WARN",
						"poller_id":   pollerID,
						"error_class": errorClass(err),
					})
					time.Sleep(5 * time.Second)
					continue
				}

				for _, msg := range out.Messages {
					jobs <- msg
				}
			}
		}(pollerID)
	}

	workers.Wait()
}

func processMessage(
	ctx context.Context,
	msg sqstypes.Message,
	sqsClient queueClient,
	dynamoClient profileStore,
	s3Client *store.S3Client,
	engine scorer,
	userLocks *userLockSet,
	queueURL, resultsQueueURL, fraudAlertQueueURL string,
) error {
	started := time.Now()
	receiveCount := messageIntAttribute(msg, "ApproximateReceiveCount")
	ingestedAt, sqsAgeMS := messageSentAt(msg, started)

	var tx Transaction
	if err := json.Unmarshal([]byte(aws.ToString(msg.Body)), &tx); err != nil {
		// Malformed JSON can never succeed — delete immediately to avoid DLQ noise.
		traceID := ensureTraceID(Transaction{}, msg)
		ackStatus, ackDurationMS := deleteMessage(ctx, sqsClient, queueURL, msg.ReceiptHandle)
		logProcessor("tx_rejected", map[string]any{
			"trace_id":        traceID,
			"reason":          "invalid_json",
			"receive_count":   receiveCount,
			"sqs_age_ms":      sqsAgeMS,
			"ack_status":      ackStatus,
			"ack_duration_ms": ackDurationMS,
			"duration_ms":     durationMS(started),
		})
		return nil
	}
	tx.TraceID = ensureTraceID(tx, msg)

	if tx.TransactionID == "" || tx.UserID == "" {
		ackStatus, ackDurationMS := deleteMessage(ctx, sqsClient, queueURL, msg.ReceiptHandle)
		logProcessor("tx_rejected", map[string]any{
			"trace_id":        tx.TraceID,
			"reason":          "missing_required_fields",
			"receive_count":   receiveCount,
			"sqs_age_ms":      sqsAgeMS,
			"ack_status":      ackStatus,
			"ack_duration_ms": ackDurationMS,
			"duration_ms":     durationMS(started),
		})
		return nil
	}

	unlockUser := userLocks.lock(tx.UserID)
	defer unlockUser()

	profileStarted := time.Now()
	profile, err := dynamoClient.GetProfile(ctx, tx.UserID)
	if err != nil {
		logProcessingFailed(tx.TraceID, started, "profile_load", err, receiveCount, sqsAgeMS)
		return fmt.Errorf("dynamo GetProfile: %w", err)
	}
	profileLoadDurationMS := durationMS(profileStarted)

	scoringStarted := time.Now()
	fraudScore, isFraud := engine.score(ctx, tx, profile)
	scoringDurationMS := durationMS(scoringStarted)

	// Update the user profile with the new transaction regardless of the score.
	profileUpdateStarted := time.Now()
	if err := dynamoClient.UpdateProfile(
		ctx, profile, tx.Amount, tx.Country, tx.Channel, tx.DestinationAccount, tx.Timestamp,
	); err != nil {
		logProcessingFailed(tx.TraceID, started, "profile_update", err, receiveCount, sqsAgeMS)
		return fmt.Errorf("dynamo UpdateProfile: %w", err)
	}
	profileUpdateDurationMS := durationMS(profileUpdateStarted)

	result := ScoringResult{
		TraceID:       tx.TraceID,
		TransactionID: tx.TransactionID,
		UserID:        tx.UserID,
		Amount:        tx.Amount,
		Currency:      tx.Currency,
		Country:       tx.Country,
		Channel:       tx.Channel,
		FraudScore:    fraudScore,
		IsFraud:       isFraud,
		ProcessedAt:   time.Now().UTC().Format(time.RFC3339),
		IngestedAt:    ingestedAt,
	}

	s3Status := "disabled"
	s3DurationMS := int64(0)
	if s3Client != nil {
		audit := AuditEvent{
			Transaction:   tx,
			ScoringResult: result,
			TraceID:       tx.TraceID,
			ProcessedAt:   result.ProcessedAt,
			IngestedAt:    result.IngestedAt,
		}
		s3Started := time.Now()
		if err := s3Client.PutRawEvent(ctx, tx.TransactionID, audit); err != nil {
			s3Status = "failed"
		} else {
			s3Status = "ok"
		}
		s3DurationMS = durationMS(s3Started)
	}

	publishStarted := time.Now()
	if err := publishScoringResult(ctx, sqsClient, resultsQueueURL, fraudAlertQueueURL, result); err != nil {
		logProcessingFailed(tx.TraceID, started, "publish", err, receiveCount, sqsAgeMS)
		return err
	}
	publishDurationMS := durationMS(publishStarted)

	ackStatus, ackDurationMS := deleteMessage(ctx, sqsClient, queueURL, msg.ReceiptHandle)
	logProcessor("tx_processed", map[string]any{
		"trace_id":                   tx.TraceID,
		"receive_count":              receiveCount,
		"sqs_age_ms":                 sqsAgeMS,
		"profile_load_duration_ms":   profileLoadDurationMS,
		"profile_update_duration_ms": profileUpdateDurationMS,
		"scoring_duration_ms":        scoringDurationMS,
		"s3_status":                  s3Status,
		"s3_duration_ms":             s3DurationMS,
		"publish_status":             "ok",
		"publish_duration_ms":        publishDurationMS,
		"ack_status":                 ackStatus,
		"ack_duration_ms":            ackDurationMS,
		"duration_ms":                durationMS(started),
	})

	return nil
}

func publishScoringResult(
	ctx context.Context,
	sqsClient queueClient,
	resultsQueueURL, fraudAlertQueueURL string,
	result ScoringResult,
) error {
	payload, err := json.Marshal(result)
	if err != nil {
		return fmt.Errorf("marshal scoring result: %w", err)
	}

	if _, err := sqsClient.SendMessage(ctx, &sqs.SendMessageInput{
		QueueUrl:    aws.String(resultsQueueURL),
		MessageBody: aws.String(string(payload)),
	}); err != nil {
		return fmt.Errorf("sqs send result: %w", err)
	}

	if result.IsFraud {
		if _, err := sqsClient.SendMessage(ctx, &sqs.SendMessageInput{
			QueueUrl:    aws.String(fraudAlertQueueURL),
			MessageBody: aws.String(string(payload)),
		}); err != nil {
			return fmt.Errorf("sqs send auxiliary result: %w", err)
		}
	}

	return nil
}

// buildMLFeatures constructs the feature map expected by the Go runtime.
// All features in the contract are initialised to NaN; the model's
// MissingGoToLeft strategy handles absent fields during tree traversal.
// Derived features are computed when their source fields are present.
func buildMLFeatures(tx Transaction, featureOrder []string) map[string]float64 {
	features := make(map[string]float64, len(featureOrder))
	for _, name := range featureOrder {
		features[name] = math.NaN()
	}

	features["amount"] = tx.Amount
	features["amount_log1p"] = signedLog1p(tx.Amount)

	if tx.OldBalanceOrg != nil {
		features["oldbalance_org"] = *tx.OldBalanceOrg
		features["oldbalance_org_log1p"] = signedLog1p(*tx.OldBalanceOrg)
		if *tx.OldBalanceOrg != 0 {
			r := tx.Amount / *tx.OldBalanceOrg
			features["amount_to_oldbalance_ratio"] = r
			features["amount_to_oldbalance_ratio_bounded"] = clamp(r, -10, 10)
		}
	}

	if tx.NewBalanceOrig != nil {
		features["newbalance_orig"] = *tx.NewBalanceOrig
		features["newbalance_orig_log1p"] = signedLog1p(*tx.NewBalanceOrig)
		if *tx.NewBalanceOrig != 0 {
			r := tx.Amount / *tx.NewBalanceOrig
			features["amount_to_newbalance_ratio"] = r
			features["amount_to_newbalance_ratio_bounded"] = clamp(r, -10, 10)
		}
	}

	if tx.OldBalanceOrg != nil && tx.NewBalanceOrig != nil {
		delta := *tx.NewBalanceOrig - *tx.OldBalanceOrg
		features["balance_delta_org"] = delta
		features["balance_delta_org_log1p"] = signedLog1p(delta)
	}

	if tx.OldBalanceDest != nil {
		features["oldbalance_dest"] = *tx.OldBalanceDest
		features["oldbalance_dest_log1p"] = signedLog1p(*tx.OldBalanceDest)
		if *tx.OldBalanceDest != 0 {
			r := tx.Amount / *tx.OldBalanceDest
			features["amount_to_dest_oldbalance_ratio"] = r
			features["amount_to_dest_oldbalance_ratio_bounded"] = clamp(r, -10, 10)
		}
	}

	if tx.NewBalanceDest != nil {
		features["newbalance_dest"] = *tx.NewBalanceDest
		features["newbalance_dest_log1p"] = signedLog1p(*tx.NewBalanceDest)
		if *tx.NewBalanceDest != 0 {
			r := tx.Amount / *tx.NewBalanceDest
			features["amount_to_dest_newbalance_ratio"] = r
			features["amount_to_dest_newbalance_ratio_bounded"] = clamp(r, -10, 10)
		}
	}

	if tx.OldBalanceDest != nil && tx.NewBalanceDest != nil {
		delta := *tx.NewBalanceDest - *tx.OldBalanceDest
		features["balance_delta_dest"] = delta
		features["balance_delta_dest_log1p"] = signedLog1p(delta)
	}

	for k, v := range tx.Features {
		if _, inContract := features[k]; inContract {
			features[k] = v
		}
	}

	return features
}

func deleteMessage(ctx context.Context, sqsClient queueClient, queueURL string, receiptHandle *string) (string, int64) {
	started := time.Now()
	if _, err := sqsClient.DeleteMessage(ctx, &sqs.DeleteMessageInput{
		QueueUrl:      aws.String(queueURL),
		ReceiptHandle: receiptHandle,
	}); err != nil {
		return "failed", durationMS(started)
	}
	return "ok", durationMS(started)
}

func mustEnv(key string) string {
	v := os.Getenv(key)
	if v == "" {
		log.Fatalf("required environment variable %s is not set", key)
	}
	return v
}

func envInt(key string, defaultValue, minValue, maxValue int) int {
	raw := os.Getenv(key)
	if raw == "" {
		return defaultValue
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		log.Fatalf("environment variable %s must be an integer, got %q", key, raw)
	}
	if value < minValue {
		return minValue
	}
	if value > maxValue {
		return maxValue
	}
	return value
}

func signedLog1p(x float64) float64 {
	if x >= 0 {
		return math.Log1p(x)
	}
	return -math.Log1p(-x)
}

func clamp(x, min, max float64) float64 {
	if x < min {
		return min
	}
	if x > max {
		return max
	}
	return x
}

func ensureTraceID(tx Transaction, msg sqstypes.Message) string {
	if isSafeTraceID(tx.TraceID) {
		return strings.TrimSpace(tx.TraceID)
	}
	if tx.TransactionID != "" {
		return "legacy-" + hashIdentifier(tx.TransactionID)
	}
	if aws.ToString(msg.MessageId) != "" {
		return "sqs-" + hashIdentifier(aws.ToString(msg.MessageId))
	}
	return ""
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

func messageIntAttribute(msg sqstypes.Message, name string) int {
	value, err := strconv.Atoi(msg.Attributes[name])
	if err != nil {
		return 0
	}
	return value
}

func messageSentAt(msg sqstypes.Message, now time.Time) (string, int64) {
	raw := msg.Attributes["SentTimestamp"]
	if raw == "" {
		return "", 0
	}
	ms, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		return "", 0
	}
	sentAt := time.UnixMilli(ms).UTC()
	age := now.Sub(sentAt).Milliseconds()
	if age < 0 {
		age = 0
	}
	return sentAt.Format(time.RFC3339), age
}

func durationMS(started time.Time) int64 {
	return time.Since(started).Milliseconds()
}

func logProcessingFailed(traceID string, started time.Time, stage string, err error, receiveCount int, sqsAgeMS int64) {
	logProcessor("tx_processing_failed", map[string]any{
		"level":         "WARN",
		"trace_id":      traceID,
		"failure_stage": stage,
		"error_class":   errorClass(err),
		"receive_count": receiveCount,
		"sqs_age_ms":    sqsAgeMS,
		"duration_ms":   durationMS(started),
	})
}

func logProcessor(action string, fields map[string]any) {
	record := processorLogRecord(action, fields)
	encoded, err := json.Marshal(record)
	if err != nil {
		log.Printf(`{"level":"ERROR","component":"processor","action":"log_encode_failed"}`)
		return
	}
	log.Print(string(encoded))
}

func processorLogRecord(action string, fields map[string]any) map[string]any {
	level := "INFO"
	if fields != nil {
		if customLevel, ok := fields["level"].(string); ok && customLevel != "" {
			level = customLevel
		}
	}
	record := map[string]any{
		"level":     level,
		"component": "processor",
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

func errorClass(err error) string {
	if err == nil {
		return ""
	}
	return fmt.Sprintf("%T", err)
}
