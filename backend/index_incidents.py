from app.services.vector_store_service import index_incidents


if __name__ == "__main__":
    count = index_incidents()
    print(f"Indexed incidents: {count}")