package writer

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"
)

type Runtime struct {
	db         *sql.DB
	client     *http.Client
	logger     *log.Logger
	runtimeAPI string
}

func NewRuntime(db *sql.DB, logger *log.Logger) (*Runtime, error) {
	runtimeAPI := os.Getenv("AWS_LAMBDA_RUNTIME_API")
	if runtimeAPI == "" {
		return nil, fmt.Errorf("AWS_LAMBDA_RUNTIME_API is not set")
	}

	if logger == nil {
		logger = log.Default()
	}

	return &Runtime{
		db:         db,
		client:     &http.Client{Timeout: 0},
		logger:     logger,
		runtimeAPI: runtimeAPI,
	}, nil
}

func (r *Runtime) Serve(ctx context.Context) error {
	for {
		requestID, deadline, event, err := r.nextInvocation(ctx)
		if err != nil {
			return err
		}

		invokeCtx := ctx
		cancel := func() {}
		if !deadline.IsZero() {
			invokeCtx, cancel = context.WithDeadline(ctx, deadline)
		}

		result, err := HandleWithRequest(invokeCtx, r.db, event, requestID, r.logger)
		cancel()
		if err != nil {
			logWriter(r.logger, "results_batch_failed", map[string]any{
				"level":          "WARN",
				"aws_request_id": requestID,
				"error_class":    errorClass(err),
			})
			if postErr := r.postInvocationError(requestID, err); postErr != nil {
				return postErr
			}
			continue
		}

		if err := r.postInvocationResponse(requestID, result); err != nil {
			return err
		}
	}
}

func (r *Runtime) nextInvocation(ctx context.Context) (string, time.Time, Event, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+r.runtimeAPI+"/2018-06-01/runtime/invocation/next", nil)
	if err != nil {
		return "", time.Time{}, Event{}, fmt.Errorf("build next invocation request: %w", err)
	}

	resp, err := r.client.Do(req)
	if err != nil {
		return "", time.Time{}, Event{}, fmt.Errorf("fetch next invocation: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", time.Time{}, Event{}, fmt.Errorf("runtime api returned %s", resp.Status)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", time.Time{}, Event{}, fmt.Errorf("read event body: %w", err)
	}

	var event Event
	if err := json.Unmarshal(body, &event); err != nil {
		return "", time.Time{}, Event{}, fmt.Errorf("decode event body: %w", err)
	}

	requestID := resp.Header.Get("Lambda-Runtime-Aws-Request-Id")
	if requestID == "" {
		return "", time.Time{}, Event{}, fmt.Errorf("runtime api response missing request id")
	}

	deadline, err := parseDeadline(resp.Header.Get("Lambda-Runtime-Deadline-Ms"))
	if err != nil {
		return "", time.Time{}, Event{}, fmt.Errorf("parse deadline: %w", err)
	}

	return requestID, deadline, event, nil
}

func (r *Runtime) postInvocationResponse(requestID string, result Result) error {
	body, err := json.Marshal(result)
	if err != nil {
		return fmt.Errorf("marshal invocation response: %w", err)
	}

	req, err := http.NewRequest(http.MethodPost, "http://"+r.runtimeAPI+"/2018-06-01/runtime/invocation/"+requestID+"/response", bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("build response request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := r.client.Do(req)
	if err != nil {
		return fmt.Errorf("post invocation response: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode/100 != 2 {
		snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return fmt.Errorf("runtime response failed: %s: %s", resp.Status, string(snippet))
	}

	return nil
}

func (r *Runtime) postInvocationError(requestID string, err error) error {
	payload := map[string]string{
		"errorMessage": err.Error(),
		"errorType":    "ResultsWriterError",
	}
	body, marshalErr := json.Marshal(payload)
	if marshalErr != nil {
		return fmt.Errorf("marshal invocation error: %w", marshalErr)
	}

	req, reqErr := http.NewRequest(http.MethodPost, "http://"+r.runtimeAPI+"/2018-06-01/runtime/invocation/"+requestID+"/error", bytes.NewReader(body))
	if reqErr != nil {
		return fmt.Errorf("build error request: %w", reqErr)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, doErr := r.client.Do(req)
	if doErr != nil {
		return fmt.Errorf("post invocation error: %w", doErr)
	}
	defer resp.Body.Close()

	if resp.StatusCode/100 != 2 {
		snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return fmt.Errorf("runtime error response failed: %s: %s", resp.Status, string(snippet))
	}

	return nil
}

func parseDeadline(raw string) (time.Time, error) {
	if raw == "" {
		return time.Time{}, nil
	}

	ms, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		return time.Time{}, err
	}

	return time.UnixMilli(ms), nil
}
