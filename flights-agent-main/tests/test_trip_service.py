from services.trip_service import TripStore
from datetime import datetime,timezone,timedelta

def test_trip_store_persists_across_instances(tmp_path):
    path=tmp_path/'trips.sqlite3'; first=TripStore(path); saved=first.save({'fare':{'price':12}},'Test')
    second=TripStore(path)
    assert second.get(saved['trip_id'])['payload']['fare']['price']==12
    assert len(second.list())==1

def test_saved_trip_exposes_historical_evidence_without_rewriting_status(tmp_path):
    store=TripStore(tmp_path/'trips.sqlite3')
    saved=store.save({'selected_itinerary':{'retrieved_at':'2020-01-01T00:00:00+00:00','trust_status':{'overall':'FARE ONLY'}}})
    loaded=store.get(saved['trip_id'])
    assert loaded['evidence_age_status']=='HISTORICAL EVIDENCE — REVALIDATION REQUIRED'
    assert loaded['payload']['selected_itinerary']['trust_status']['overall']=='FARE ONLY'
    assert loaded['original_evidence_status']=='FARE ONLY'
    assert loaded['current_validation_status']=='not_revalidated'
    assert loaded['revalidation_occurred'] is False
    assert loaded['evidence_age_by_source']['fare']=='HISTORICAL EVIDENCE — REVALIDATION REQUIRED'
    assert loaded['evidence_age_by_source']['schedule']=='FRESHNESS NOT ESTABLISHED'

def test_saved_trip_without_retrieval_timestamp_marks_freshness_unknown(tmp_path):
    store=TripStore(tmp_path/'trips.sqlite3')
    loaded=store.save({'searched_at':datetime.now(timezone.utc).isoformat(),'selected_itinerary':{'price_status':'unverified'}})
    reopened=TripStore(tmp_path/'trips.sqlite3').get(loaded['trip_id'])
    assert reopened['evidence_age_status']=='FRESHNESS NOT ESTABLISHED'
    assert reopened['original_evidence_status']=='unverified'
    assert reopened['evidence_captured_at'] is None
    assert reopened['revalidation_occurred'] is False

def test_newly_saved_matched_fare_and_schedule_show_source_ages(tmp_path):
    stamp=datetime.now(timezone.utc).isoformat()
    store=TripStore(tmp_path/'trips.sqlite3')
    saved=store.save({'selected_itinerary':{'retrieved_at':stamp,'schedule_retrieved_at':stamp,'schedule_matched':True,'trust_status':{'overall':'MATCHED FARE AND SCHEDULE','schedule':'SCHEDULE CONFIRMED'}}})
    loaded=TripStore(tmp_path/'trips.sqlite3').get(saved['trip_id'])
    assert loaded['evidence_age_by_source']=={'fare':'RECENTLY RETRIEVED','schedule':'RECENTLY RETRIEVED'}
    assert loaded['evidence_age_status']=='RECENTLY RETRIEVED'
    assert loaded['original_schedule_status']=='SCHEDULE CONFIRMED'

def test_saved_fare_only_keeps_schedule_age_unestablished(tmp_path):
    stamp=datetime.now(timezone.utc).isoformat()
    store=TripStore(tmp_path/'trips.sqlite3')
    saved=store.save({'selected_itinerary':{'retrieved_at':stamp,'schedule_matched':False,'price_status':'verified','trust_status':{'overall':'FARE ONLY','schedule':'SCHEDULE UNCONFIRMED'}}})
    loaded=store.get(saved['trip_id'])
    assert loaded['evidence_age_by_source']['fare']=='RECENTLY RETRIEVED'
    assert loaded['evidence_age_by_source']['schedule']=='FRESHNESS NOT ESTABLISHED'
    assert loaded['original_evidence_status']=='FARE ONLY'
    assert loaded['original_schedule_status']=='SCHEDULE UNCONFIRMED'

def test_old_fare_with_recent_schedule_is_still_historical_and_unmodified(tmp_path):
    old=(datetime.now(timezone.utc)-timedelta(days=45)).isoformat()
    recent=datetime.now(timezone.utc).isoformat()
    store=TripStore(tmp_path/'trips.sqlite3')
    saved=store.save({'selected_itinerary':{'retrieved_at':old,'schedule_retrieved_at':recent,'schedule_matched':True,'trust_status':{'overall':'MATCHED FARE AND SCHEDULE','schedule':'SCHEDULE CONFIRMED'}}})
    loaded=TripStore(tmp_path/'trips.sqlite3').get(saved['trip_id'])
    assert loaded['evidence_age_status']=='HISTORICAL EVIDENCE — REVALIDATION REQUIRED'
    assert loaded['payload']['selected_itinerary']['trust_status']['overall']=='MATCHED FARE AND SCHEDULE'
    assert loaded['current_validation_status']=='not_revalidated'
