import React from 'react';

export function useListenerSummaryRequestGuards() {
  const summaryRequestSeq = React.useRef(0);
  const summaryActionSeq = React.useRef(0);
  const loadRequestSeq = React.useRef(0);
  const actionRequestSeq = React.useRef(0);

  function beginSummaryRequest() {
    summaryRequestSeq.current += 1;
    return summaryRequestSeq.current;
  }

  function currentSummaryActionSeq() {
    return summaryActionSeq.current;
  }

  function isActiveSummaryRequest(requestSeq: number, actionSeq: number) {
    return summaryRequestSeq.current === requestSeq && summaryActionSeq.current === actionSeq;
  }

  function beginSummaryAction() {
    summaryActionSeq.current += 1;
    return summaryActionSeq.current;
  }

  function isActiveSummaryAction(actionSeq: number) {
    return summaryActionSeq.current === actionSeq;
  }

  function beginLoadRequest() {
    loadRequestSeq.current += 1;
    return loadRequestSeq.current;
  }

  function isActiveLoadRequest(requestSeq: number) {
    return loadRequestSeq.current === requestSeq;
  }

  function beginActionRequest() {
    actionRequestSeq.current += 1;
    return actionRequestSeq.current;
  }

  function isActiveActionRequest(requestSeq: number) {
    return actionRequestSeq.current === requestSeq;
  }

  return {
    beginActionRequest,
    beginLoadRequest,
    beginSummaryAction,
    beginSummaryRequest,
    currentSummaryActionSeq,
    isActiveActionRequest,
    isActiveLoadRequest,
    isActiveSummaryAction,
    isActiveSummaryRequest,
  };
}

export function useListenerDetailRequestGuards() {
  const detailRequestSeq = React.useRef(0);

  function beginDetailRequest() {
    detailRequestSeq.current += 1;
    return detailRequestSeq.current;
  }

  function isActiveDetailRequest(requestSeq: number) {
    return detailRequestSeq.current === requestSeq;
  }

  return {
    beginDetailRequest,
    isActiveDetailRequest,
  };
}
